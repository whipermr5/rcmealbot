import webapp2
import logging
import json
import textwrap
import xlrd
import ast
import parsedatetime
from google.appengine.api import urlfetch, urlfetch_errors, taskqueue
from google.appengine.ext import db
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from secrets import TOKEN, ADMIN_ID
TELEGRAM_URL = 'https://api.telegram.org/bot' + TOKEN
TELEGRAM_URL_SEND = TELEGRAM_URL + '/sendMessage'
TELEGRAM_URL_CHAT_ACTION = TELEGRAM_URL + '/sendChatAction'
JSON_HEADER = {'Content-Type': 'application/json;charset=utf-8'}

BASE_URL = 'https://myaces.nus.edu.sg/Prjhml/'
UNAUTHORISED = 'empty'
SESSION_EXPIRED = 'Sorry {}, your session has expired. Please /login again'

LOG_SENT = '{} {} sent to uid {} ({})'
LOG_AUTH = 'Authenticating with jsessionid '
LOG_AUTH_FAILED = 'Authentication failed for uid {} ({})'
LOG_AUTH_SUCCESS = 'Successfully authenticated as {} ({})'
LOG_ENQUEUED = 'Enqueued {} to uid {} ({})'
LOG_DID_NOT_SEND = 'Did not send {} to uid {} ({}): {}'
LOG_ERROR_SENDING = 'Error sending {} to uid {} ({}):\n{}'
LOG_ERROR_DATASTORE = 'Error reading from datastore:\n'
LOG_ERROR_REMOTE = 'Error accessing site:\n'
LOG_ERROR_AUTH = 'Error sending auth request for uid {} ({})'
LOG_TYPE_START_NEW = 'Type: Start (new user)'
LOG_TYPE_START_EXISTING = 'Type: Start (existing user)'
LOG_TYPE_NON_TEXT = 'Type: Non-text'
LOG_TYPE_COMMAND = 'Type: Command\n'
LOG_UNRECOGNISED = 'Unrecognised command'
LOG_SESSION_ALIVE = 'User {} is still authenticated'
LOG_SESSION_EXPIRED = 'Session expired for user {}'

RECOGNISED_ERRORS = ('[Error]: PEER_ID_INVALID',
                     '[Error]: Bot was kicked from a chat',
                     '[Error]: Bot was blocked by the user',
                     '[Error]: Bad Request: chat not found',
                     '[Error]: Forbidden: can\'t write to chat with deleted user',
                     '[Error]: Forbidden: can\'t write to private chat with deleted user')

def get_new_jsessionid():
    url = BASE_URL + 'login.do'

    try:
        result = urlfetch.fetch(url, deadline=10)
    except urlfetch_errors.Error as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None

    html = result.content
    idx = html.find('jsessionid=') + 11
    jsessionid = html[idx:idx+33]
    return jsessionid

def check_auth(jsessionid):
    url = BASE_URL + 'studstaffMealBalance.do;jsessionid=' + jsessionid
    logging.info(LOG_AUTH + jsessionid)

    try:
        result = urlfetch.fetch(url, method=urlfetch.HEAD, follow_redirects=False, deadline=10)
    except urlfetch_errors.Error as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None

    return result.status_code == 200

def check_meals(jsessionid, first_time_user=None, get_excel=False):
    url = BASE_URL + 'studstaffMealBalance.do;jsessionid=' + jsessionid
    logging.info(LOG_AUTH + jsessionid)

    try:
        result = urlfetch.fetch(url, follow_redirects=False, deadline=10)
    except urlfetch_errors.Error as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None

    if result.status_code != 200:
        return UNAUTHORISED

    html = result.content

    if get_excel:
        start = html.find('<div class="exportlinks"> Export As: ') + 47
        end = html.find('&amp;', start)
        link = html[start:end]
        xls_url = BASE_URL + link

        try:
            xls_result = urlfetch.fetch(xls_url, follow_redirects=False, deadline=10)
        except urlfetch_errors.Error as e:
            logging.warning(LOG_ERROR_REMOTE + str(e))
            return None

        if xls_result.status_code != 200:
            return UNAUTHORISED

        return xls_result.content

    elif first_time_user:
        start = html.find('<td colspan="3">') + 16
        end = html.find('</td>', start)
        full_name = html[start:end].replace('&nbsp;', ' ').strip()

        start = html.find('<td colspan="3">', end) + 16
        end = html.find('</td>', start)
        matric = html[start:end].replace('&nbsp;', ' ').strip()

        start = html.find('<td colspan="3">', end) + 16
        end = html.find('</td>', start)
        meal_pref = html[start:end].replace('&nbsp;', ' ').strip()

        first_time_user.full_name = full_name
        first_time_user.matric = matric
        first_time_user.meal_pref = meal_pref
        first_time_user.put()

        logging.info(LOG_AUTH_SUCCESS.format(full_name, matric))
        return 'Success! You are logged in as *{}* _({})_.\n\n'.format(full_name, matric)

    def summarise(html):
        data = ''.join(html.replace('/', '').replace('<td>', ' ')).split()
        return 'Consumed: {}\nForfeited: {}\nCarried forward: {}\nTotal remaining: {}'.format(data[1], data[2], data[3], data[5])

    start = html.find('<td class="fieldname" nowrap="true"> Breakfast </td>') + 75
    end = html.find('</tr>', start)
    breakfast = html[start:end]

    start = html.find('<td class="fieldname" nowrap="true"> Dinner </td>') + 72
    end = html.find('</tr>', start)
    dinner = html[start:end]

    return '*Breakfast*\n' + summarise(breakfast) + '\n\n*Dinner*\n' + summarise(dinner)

def weekly_summary(xls_data):
    def describe(number_of_meals, meal_type):
        if number_of_meals == 0:
            description = 'no {}s'.format(meal_type)
        elif number_of_meals == 1:
            description = '1 {}'.format(meal_type)
        else:
            description = '{} {}s'.format(number_of_meals, meal_type)
        return description

    breakfasts = 0
    dinners = 0

    sh = xlrd.open_workbook(file_contents=xls_data).sheet_by_index(0)
    for i in range(1, sh.nrows):
        date = datetime.strptime(sh.row(i)[1].value, '%d/%m/%Y %H:%M:%S')
        week = date.strftime('%Y-W%W')
        this_week = get_today_time().strftime('%Y-W%W')
        if week != this_week:
            break
        meal_type = sh.row(i)[2].value
        if meal_type == u'Breakfast':
            breakfasts += 1
        elif meal_type == u'Dinner':
            dinners += 1

    overall_description = describe(breakfasts + dinners, 'meal')
    breakfast_description = describe(breakfasts, 'breakfast')
    dinner_description = describe(dinners, 'dinner')

    return '{} ({} and {})'.format(overall_description, breakfast_description, dinner_description)

def telegram_post(data, deadline=3):
    return urlfetch.fetch(url=TELEGRAM_URL_SEND, payload=data, method=urlfetch.POST,
                          headers=JSON_HEADER, deadline=deadline)

def get_today_time():
    today = (datetime.utcnow() + timedelta(hours=8)).date()
    today_time = datetime(today.year, today.month, today.day) - timedelta(hours=8)
    return today_time

class User(db.Model):
    username = db.StringProperty(indexed=False)
    first_name = db.StringProperty(multiline=True, indexed=False)
    last_name = db.StringProperty(multiline=True, indexed=False)
    created = db.DateTimeProperty(auto_now_add=True)
    last_received = db.DateTimeProperty(auto_now_add=True, indexed=False)
    last_sent = db.DateTimeProperty(indexed=False)
    last_auto = db.DateTimeProperty(auto_now_add=True)
    last_weekly = db.DateTimeProperty(auto_now_add=True)
    active = db.BooleanProperty(default=True)
    active_weekly = db.BooleanProperty(default=True)

    jsessionid = db.StringProperty(indexed=False)
    auth = db.BooleanProperty(default=False)

    full_name = db.StringProperty(indexed=False)
    matric = db.StringProperty(indexed=False)
    meal_pref = db.StringProperty(indexed=False)

    def get_uid(self):
        return self.key().name()

    def get_first_name(self):
        return self.first_name.encode('utf-8', 'ignore').strip()

    def get_name_string(self):
        def prep(string):
            return string.encode('utf-8', 'ignore').strip()

        name = prep(self.first_name)
        if self.last_name:
            name += ' ' + prep(self.last_name)
        if self.username:
            name += ' @' + prep(self.username)

        return name

    def is_active(self):
        return self.active

    def is_active_weekly(self):
        return self.active_weekly

    def is_authenticated(self):
        return self.auth

    def set_active(self, active):
        self.active = active
        self.put()

    def set_active_weekly(self, active_weekly):
        self.active_weekly = active_weekly
        self.put()

    def set_authenticated(self, auth):
        self.auth = auth
        if not auth:
            self.jsessionid = None
        self.put()

    def set_jsessionid(self, jsessionid):
        self.jsessionid = jsessionid
        self.put()

    def update_last_received(self):
        self.last_received = datetime.now()
        self.put()

    def update_last_sent(self):
        self.last_sent = datetime.now()
        self.put()

    def update_last_auto(self):
        self.last_auto = get_today_time()
        self.put()

    def update_last_weekly(self):
        self.last_weekly = get_today_time()
        self.put()

class Data(db.Model):
    menus = db.TextProperty()
    start_date = db.DateProperty(indexed=False)

def get_user(uid):
    key = db.Key.from_path('User', str(uid))
    user = db.get(key)
    if user == None:
        user = User(key_name=str(uid), first_name='-')
        user.put()
    return user

def get_data():
    key = db.Key.from_path('Data', 'main')
    data = db.get(key)
    if data == None:
        data = Data(key_name='main')
        data.put()
    return data

def update_profile(uid, uname, fname, lname):
    existing_user = get_user(uid)
    if existing_user:
        existing_user.username = uname
        existing_user.first_name = fname
        existing_user.last_name = lname
        existing_user.update_last_received()
        #existing_user.put()
        return existing_user
    else:
        user = User(key_name=str(uid), username=uname, first_name=fname, last_name=lname)
        user.put()
        return user

def send_message(user_or_uid, text, msg_type='message', force_reply=False, markdown=False, disable_web_page_preview=True):
    try:
        uid = str(user_or_uid.get_uid())
        user = user_or_uid
    except AttributeError:
        uid = str(user_or_uid)
        user = get_user(user_or_uid)

    def send_short_message(text, countdown=0):
        build = {
            'chat_id': uid,
            'text': text
        }

        if force_reply:
            build['reply_markup'] = {'force_reply': True}
        if markdown:
            build['parse_mode'] = 'Markdown'
        if disable_web_page_preview:
            build['disable_web_page_preview'] = True

        data = json.dumps(build)

        def queue_message():
            payload = json.dumps({
                'msg_type': msg_type,
                'data': data
            })
            taskqueue.add(url='/message', payload=payload, countdown=countdown)
            logging.info(LOG_ENQUEUED.format(msg_type, uid, user.get_name_string()))

        if msg_type in ('daily', 'weekly', 'mass'):
            if msg_type == 'daily':
                user.update_last_auto()
            elif msg_type == 'weekly':
                user.update_last_weekly()

            queue_message()
            return

        try:
            result = telegram_post(data)
        except urlfetch_errors.Error as e:
            logging.warning(LOG_ERROR_SENDING.format(msg_type, uid, user.get_name_string(), str(e)))
            queue_message()
            return

        response = json.loads(result.content)
        error_description = str(response.get('description'))

        if error_description.startswith('[Error]: Bad Request: can\'t parse message'):
            if build.get('parse_mode'):
                del build['parse_mode']
            data = json.dumps(build)
            queue_message()

        elif handle_response(response, user, uid, msg_type) == False:
            queue_message()

    if len(text) > 4096:
        chunks = textwrap.wrap(text, width=4096, replace_whitespace=False, drop_whitespace=False)
        i = 0
        for chunk in chunks:
            send_short_message(chunk, i)
            i += 1
    else:
        send_short_message(text)

def handle_response(response, user, uid, msg_type):
    if response.get('ok') == True:
        msg_id = str(response.get('result').get('message_id'))
        logging.info(LOG_SENT.format(msg_type.capitalize(), msg_id, uid, user.get_name_string()))
        user.update_last_sent()

    else:
        error_description = str(response.get('description'))
        if error_description not in RECOGNISED_ERRORS:
            logging.warning(LOG_ERROR_SENDING.format(msg_type, uid, user.get_name_string(),
                                                     error_description))
            return False

        logging.info(LOG_DID_NOT_SEND.format(msg_type, uid, user.get_name_string(),
                                             error_description))

        user.set_active(False)
        user.set_active_weekly(False)
        if msg_type == 'promo':
            user.set_promo(False)

    return True

def send_typing(uid):
    data = json.dumps({'chat_id': uid, 'action': 'typing'})
    try:
        rpc = urlfetch.create_rpc()
        urlfetch.make_fetch_call(rpc, url=TELEGRAM_URL_CHAT_ACTION, payload=data,
                                 method=urlfetch.POST, headers=JSON_HEADER)
    except urlfetch_errors.Error:
        return

class MainPage(webapp2.RequestHandler):
    WELCOME = 'Hello, {}! Welcome! To get started, enter one of the following commands:\n\n'
    HELP = 'Hi {}! Please enter one of the following commands:\n\n'
    ABOUT = 'Created by @whipermr5. Comments, feedback and suggestions are welcome!\n\n' + \
            'Food menu extracted from http://nus.edu.sg/ohs/current-residents/students/dining-daily.php\n\n' + \
            'P.S. CAPT rocks! And God loves you :)'
    UNRECOGNISED = 'Sorry {}, I couldn\'t understand that. ' + \
                   'Please enter one of the following commands:\n\n'
    REMOTE_ERROR = 'Sorry {}, I\'m having some difficulty accessing the site. ' + \
                   'Please try again later.'

    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('RCMealBot backend running...\n')

    def post(self):
        def build_command_list():
            cmds = '/checkmeals - check meal credits' if user.is_authenticated() else '/login - to check meal credits'
            cmds += '\n/checkmenu - view today\'s menu'
            cmds += '\n/checkmenu <date> - view the menu for a particular day'
            cmds += '\n/settings - turn on/off automatic updates'
            cmds += '\n/about - about this bot'
            cmds += '\n/logout' if user.is_authenticated() else ''
            return cmds

        def build_settings_list():
            cmds = 'Hi, {}!'.format(user.get_first_name())
            if user.is_authenticated():
                cmds += ' You are logged in as *{}* _({})_.'.format(user.full_name, user.matric)
                cmds += ' Weekly meal reports (sent on Sunday nights) are *' + ('on' if user.is_active_weekly() else 'off') + '*.'
            else:
                cmds += ' You are *not* logged in.'
            cmds += ' Daily menu updates (sent at midnight) are *' + ('on' if user.is_active() else 'off') + '*.\n\n'
            cmds += '/weeklyoff - turn off weekly meal reports' if user.is_active_weekly() else '/weeklyon - turn on weekly meal reports'
            cmds += '\n/dailyoff - turn off daily menu updates' if user.is_active() else '\n/dailyon - turn on daily menu updates'
            return cmds

        def is_command(word):
            return cmd.startswith('/' + word)

        data = json.loads(self.request.body)
        logging.debug(self.request.body)

        msg = data.get('message')
        msg_chat = msg.get('chat')
        msg_from = msg.get('from')

        if msg_chat.get('type') == 'private':
            uid = msg_from.get('id')
            first_name = msg_from.get('first_name')
            last_name = msg_from.get('last_name')
            username = msg_from.get('username')
        else:
            uid = msg_chat.get('id')
            first_name = msg_chat.get('title')
            last_name = None
            username = None

        user = update_profile(uid, username, first_name, last_name)

        first_name = first_name.encode('utf-8', 'ignore').strip()
        if username:
            username = username.encode('utf-8', 'ignore').strip()
        if last_name:
            last_name = last_name.encode('utf-8', 'ignore').strip()
        text = msg.get('text')
        if text:
            text = text.encode('utf-8', 'ignore')

        if user.last_sent == None or text == '/start':
            if user.last_sent == None:
                logging.info(LOG_TYPE_START_NEW)
                new_user = True
            else:
                logging.info(LOG_TYPE_START_EXISTING)
                new_user = False

            send_message(user, self.WELCOME.format(first_name) + build_command_list())

            if new_user:
                send_message(ADMIN_ID, 'New user: ' + user.get_name_string())

            return

        if text == None:
            logging.info(LOG_TYPE_NON_TEXT)
            return

        logging.info(LOG_TYPE_COMMAND + text)

        cmd = text.lower().strip()

        if is_command('checkmeals'):
            if not user.is_authenticated():
                send_message(user, 'Did you mean to /login?')
                return

            send_typing(uid)
            xls_data = check_meals(user.jsessionid, get_excel=True)
            meals = check_meals(user.jsessionid)

            if not xls_data or not meals:
                send_message(user, self.REMOTE_ERROR.format(first_name))
                return
            elif xls_data == UNAUTHORISED or meals == UNAUTHORISED:
                user.set_authenticated(False)
                send_message(user, SESSION_EXPIRED.format(first_name))
                return

            send_message(user, 'You\'ve had ' + weekly_summary(xls_data) + ' this week.\n\n' + meals, markdown=True)

        elif is_command('login'):
            if user.is_authenticated():
                response = 'You are already logged in as *{}* _({})_. Did you mean to /logout?'.format(user.full_name, user.matric)
                send_message(user, response, markdown=True)
                return

            send_typing(uid)
            jsessionid = get_new_jsessionid()

            if not jsessionid:
                send_message(user, self.REMOTE_ERROR.format(first_name))
                return

            url = BASE_URL + 'login.do;jsessionid=' + jsessionid
            response = 'Login here: ' + url + '\n\nWhen done, come back here and type /continue'

            user.set_jsessionid(jsessionid)
            send_message(user, response)

        elif is_command('continue'):
            if not user.jsessionid:
                send_message(user, 'Sorry {}, please /login first'.format(first_name))
                return

            send_typing(uid)
            welcome = check_meals(user.jsessionid, first_time_user=user)

            if not welcome:
                send_message(user, self.REMOTE_ERROR.format(first_name))
                return
            elif welcome == UNAUTHORISED:
                user.set_authenticated(False)
                logging.info(LOG_AUTH_FAILED.format(uid, user.get_name_string()))
                response = 'Sorry {}, that didn\'t work. Please try /login again or, if the problem persists, read on:\n\n'.format(first_name)
                response += 'The link must be opened in a fresh browser that has never been used to browse the RC dining portal before. ' + \
                            'Try one of the following:\n'
                response += '- open the link in a new incognito window\n'
                response += '- clear the cookies in your current browser before opening the link\n'
                response += '- open the link with another browser (one you have never used to browse the RC dining portal before)\n'
                send_message(user, response)
                return

            user.set_authenticated(True)
            send_message(user, welcome, markdown=True)

            send_typing(uid)
            xls_data = check_meals(user.jsessionid, get_excel=True)
            meals = check_meals(user.jsessionid)

            if not xls_data or not meals or xls_data == UNAUTHORISED or meals == UNAUTHORISED:
                return

            send_message(user, 'You\'ve had ' + weekly_summary(xls_data) + ' this week.\n\n' + meals, markdown=True)

        elif is_command('checkmenu'):
            if len(cmd) > 10:
                date_arg = cmd[10:].strip()
                today_date = parsedatetime.Calendar().parseDT(date_arg, datetime.utcnow() + timedelta(hours=8))[0].date()
            else:
                today_date = (datetime.utcnow() + timedelta(hours=8)).date()
            menus = ast.literal_eval(get_data().menus)
            max_day = len(menus)
            start_date = get_data().start_date
            day = (today_date - start_date).days
            friendly_date = today_date.strftime('%d %B %Y (%a)')
            if day < 0 or day >= max_day:
                send_message(user, 'Sorry {}, OHS has not uploaded the menu for {} yet'.format(first_name, friendly_date))
            else:
                send_message(user, 'Menu for {}:\n\n'.format(friendly_date) + menus[day], markdown=True)

        elif is_command('settings'):
            send_message(user, build_settings_list(), markdown=True)

        elif is_command('weeklyoff'):
            if not user.is_active_weekly():
                send_message(user, 'Weekly meal reports are already off.')
                return

            user.set_active_weekly(False)
            send_message(user, 'Success! You will no longer receive weekly meal reports.')

        elif is_command('weeklyon'):
            if user.is_active_weekly():
                send_message(user, 'Weekly meal reports are already on.')
                return

            user.set_active_weekly(True)
            send_message(user, 'Success! You will receive meal reports every Sunday night.')

        elif is_command('dailyoff'):
            if not user.is_active():
                send_message(user, 'Daily menu updates are already off.')
                return

            user.set_active(False)
            send_message(user, 'Success! You will no longer receive daily menu updates.')

        elif is_command('dailyon'):
            if user.is_active():
                send_message(user, 'Daily menu updates are already on.')
                return

            user.set_active(True)
            send_message(user, 'Success! You will receive menu updates every day at midnight.')

        elif is_command('help'):
            send_message(user, self.HELP.format(first_name) + build_command_list())

        elif is_command('about'):
            send_message(user, self.ABOUT)

        elif is_command('logout'):
            if not user.is_authenticated():
                send_message(user, 'Did you mean to /login?')
                return

            user.set_authenticated(False)
            send_message(user, 'You have successfully logged out. /login again?')

        else:
            logging.info(LOG_UNRECOGNISED)
            send_message(user, self.UNRECOGNISED.format(first_name) + build_command_list())

class DailyPage(webapp2.RequestHandler):
    def run(self):
        today_date = (datetime.utcnow() + timedelta(hours=8)).date()
        menus = ast.literal_eval(get_data().menus)
        max_day = len(menus)
        start_date = get_data().start_date
        day = (today_date - start_date).days
        friendly_date = today_date.strftime('%d %B %Y (%a)')
        if day < 0 or day >= max_day:
            return True
        menu = 'Menu for {}:\n\n'.format(friendly_date) + menus[day]

        query = User.all()
        query.filter('active =', True)
        query.filter('last_auto <', get_today_time())

        try:
            for user in query.run(batch_size=500):
                send_message(user, menu, msg_type='daily', markdown=True)
        except db.Error as e:
            logging.warning(LOG_ERROR_DATASTORE + str(e))
            return False

        return True

    def get(self):
        if self.run() == False:
            taskqueue.add(url='/daily')

    def post(self):
        if self.run() == False:
            self.abort(502)

class WeeklyPage(webapp2.RequestHandler):
    def run(self):
        query = User.all()
        query.filter('auth =', True)
        query.filter('active_weekly =', True)
        query.filter('last_weekly <', get_today_time())

        try:
            for user in query.run(batch_size=500):

                xls_data = check_meals(user.jsessionid, get_excel=True)
                meals = check_meals(user.jsessionid)

                if not xls_data or not meals:
                    self.abort(502)
                elif xls_data == UNAUTHORISED or meals == UNAUTHORISED:
                    user.set_authenticated(False)
                    send_message(user, SESSION_EXPIRED.format(user.get_first_name()))
                    continue

                summary = '*Weekly Summary*\nYou had ' + weekly_summary(xls_data) + ' this week.\n\n' + meals
                send_message(user, summary, msg_type='weekly', markdown=True)
        except db.Error as e:
            logging.warning(LOG_ERROR_DATASTORE + str(e))
            return False

        return True

    def get(self):
        if self.run() == False:
            taskqueue.add(url='/weekly')

    def post(self):
        if self.run() == False:
            self.abort(502)

class MessagePage(webapp2.RequestHandler):
    def post(self):
        params = json.loads(self.request.body)
        msg_type = params.get('msg_type')
        data = params.get('data')
        uid = str(json.loads(data).get('chat_id'))
        user = get_user(uid)

        try:
            result = telegram_post(data, 4)
        except urlfetch_errors.Error as e:
            logging.warning(LOG_ERROR_SENDING.format(msg_type, uid, user.get_name_string(), str(e)))
            logging.debug(data)
            self.abort(502)

        response = json.loads(result.content)

        if handle_response(response, user, uid, msg_type) == False:
            logging.debug(data)
            self.abort(502)

class AuthPage(webapp2.RequestHandler):
    def run(self):
        query = User.all()
        query.filter('auth =', True)

        def queue_reauth(user):
            uid = user.get_uid()
            taskqueue.add(url='/reauth', payload=uid)
            logging.info(LOG_ENQUEUED.format('reauth', uid, user.get_name_string()))

        try:
            for user in query.run(batch_size=500):
                result = check_auth(user.jsessionid)
                if result:
                    logging.info(LOG_SESSION_ALIVE.format(user.get_name_string()))
                elif result == None:
                    logging.warning(LOG_ERROR_AUTH.format(user.get_uid(), user.get_name_string()))
                    queue_reauth(user)
                else:
                    logging.info(LOG_SESSION_EXPIRED.format(user.get_name_string()))
                    user.set_authenticated(False)
                    send_message(user, SESSION_EXPIRED.format(user.get_first_name()))
        except db.Error as e:
            logging.warning(LOG_ERROR_DATASTORE + str(e))
            return False

        return True

    def get(self):
        if self.run() == False:
            taskqueue.add(url='/auth')

    def post(self):
        if self.run() == False:
            self.abort(502)

class ReauthPage(webapp2.RequestHandler):
    def post(self):
        uid = self.request.body
        user = get_user(uid)

        result = check_auth(user.jsessionid)
        if result:
            logging.info(LOG_SESSION_ALIVE.format(user.get_name_string()))
        elif result == None:
            logging.warning(LOG_ERROR_AUTH.format(user.get_uid(), user.get_name_string()))
            self.abort(502)
        else:
            logging.info(LOG_SESSION_EXPIRED.format(user.get_name_string()))
            user.set_authenticated(False)
            send_message(user, SESSION_EXPIRED.format(user.get_first_name()))

class MenuPage(webapp2.RequestHandler):
    def get(self):
        url = 'http://nus.edu.sg/ohs/current-residents/students/dining-daily.php'
        try:
            result = urlfetch.fetch(url, deadline=10)
        except urlfetch_errors.Error as e:
            logging.warning(LOG_ERROR_REMOTE + str(e))
        html = result.content
        soup = BeautifulSoup(html, 'lxml')

        for tag in soup.select('.menu-selector'):
            tag.decompose()

        for tag in soup.select('.menu-legend'):
            tag.name = 'span'
            tag.string = '\n'

        for tag in soup.select('.td-cat img'):
            text = tag.get('alt')
            if text == 'Description':
                text = 'Others'
            tag.string = '\n-\n*~ ' + text + ' ~*\n'

        for tag in soup.select('br'):
            tag.name = 'span'
            tag.string = '\n'

        for tag in soup.select('p'):
            text = tag.text.strip().replace('*', '')
            tag.string = '\n' + u'\U0001F34A' + ' _' + text + '_\n'

        start_date_text = soup.select('.day-1 h4')[0].text
        idx = start_date_text.find('\n')
        start_date_text = start_date_text[:idx]
        start_date = datetime.strptime(start_date_text, "%d %b %Y").date()

        for tag in soup.select('h4'):
            if 'breakfast' in tag.text.lower():
                text = u'\U0001F32E' + ' *Breakfast*'
            else:
                text = u'\U0001F35C' + ' *Dinner*'
            tag.string = text + '\n'

        for tag in soup.select('tr'):
            text = tag.text.strip()
            tag.string = text + '\n'

        days = len(soup.select('.day-menu'))
        menus = []
        for i in range(days):
            menu = ''
            for tag in soup(class_='day-' + str(i + 1)):
                menu += tag.text.strip() + '\n'
            menu = menu.replace('\n-\n', '\n\n').strip()
            if i % 6 == 5:
                dinner_start = menu.find(u'\U0001F35C' + ' *Dinner*')
                fruit_start = menu.find('_')
                fruit_end = menu.find('_', fruit_start + 1)
                fruit = menu[fruit_start:fruit_end + 1]
                breakfast = menu[:dinner_start] + fruit
                dinner = menu[dinner_start:]
                menus.append(breakfast)
                menus.append(dinner)
            else:
                menus.append(menu)
        days = len(menus)
        data = get_data()
        data.menus = str(menus)
        data.start_date = start_date
        data.put()
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('Done updating menu starting from {} for {} days:\n'.format(start_date_text, days) + get_data().menus)

class MigratePage(webapp2.RequestHandler):
    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('Migrate page\n')

class MassPage(webapp2.RequestHandler):
    def get(self):
        taskqueue.add(url='/mass')

    def post(self):
        pass

app = webapp2.WSGIApplication([
    ('/', MainPage),
    ('/' + TOKEN, MainPage),
    ('/daily', DailyPage),
    ('/weekly', WeeklyPage),
    ('/message', MessagePage),
    ('/auth', AuthPage),
    ('/reauth', ReauthPage),
    ('/migrate', MigratePage),
    ('/mass', MassPage),
    ('/menu', MenuPage),
], debug=True)
