import webapp2
import logging
import json
import textwrap
import xlrd
from google.appengine.api import urlfetch, urlfetch_errors, taskqueue
from google.appengine.ext import db
from datetime import datetime, timedelta

BASE_URL = 'https://myaces.nus.edu.sg/Prjhml/'
UNAUTHORISED = 'empty'

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

    try:
        result = urlfetch.fetch(url, method=urlfetch.HEAD, follow_redirects=False, deadline=10)
    except urlfetch_errors.Error as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None

    return result.status_code == 200

def check_meals(jsessionid, first_time_user=None, get_excel=False):
    url = BASE_URL + 'studstaffMealBalance.do;jsessionid=' + jsessionid

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

    def summarise(html):
        data = ''.join(html.replace('/', '').replace('<td>', ' ')).split()
        return 'Consumed: {}\nForfeited: {}\nCarried forward: {}\nTotal remaining: {}'.format(data[1], data[2], data[3], data[5])

    start = html.find('<td class="fieldname" nowrap="true"> Breakfast </td>') + 75
    end = html.find('</tr>', start)
    breakfast = html[start:end]

    start = html.find('<td class="fieldname" nowrap="true"> Dinner </td>') + 72
    end = html.find('</tr>', start)
    dinner = html[start:end]

    if first_time_user:
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

        intro = 'Success! You are logged in as *{}* _({})_.\n\n'.format(full_name, matric)

    else:
        intro = ''

    return intro + '*Breakfast*\n' + summarise(breakfast) + '\n\n*Dinner*\n' + summarise(dinner)

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

    return 'You had {} ({} and {}) this week!'.format(overall_description, breakfast_description, dinner_description)

from secrets import TOKEN, ADMIN_ID
TELEGRAM_URL = 'https://api.telegram.org/bot' + TOKEN
TELEGRAM_URL_SEND = TELEGRAM_URL + '/sendMessage'
TELEGRAM_URL_CHAT_ACTION = TELEGRAM_URL + '/sendChatAction'
JSON_HEADER = {'Content-Type': 'application/json;charset=utf-8'}

LOG_SENT = '{} {} sent to uid {} ({})'
LOG_ENQUEUED = 'Enqueued {} to uid {} ({})'
LOG_DID_NOT_SEND = 'Did not send {} to uid {} ({}): {}'
LOG_ERROR_SENDING = 'Error sending {} to uid {} ({}):\n{}'
LOG_ERROR_DATASTORE = 'Error reading from datastore:\n'
LOG_ERROR_REMOTE = 'Error accessing site:\n'
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
    active = db.BooleanProperty(default=True)

    jsessionid = db.StringProperty(indexed=False)
    auth = db.BooleanProperty(default=False)

    full_name = db.StringProperty(indexed=False)
    matric = db.StringProperty(indexed=False)
    meal_pref = db.StringProperty(indexed=False)

    def get_uid(self):
        return self.key().name()

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

    def is_authenticated(self):
        return self.auth

    def set_active(self, active):
        self.active = active
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

def get_user(uid):
    key = db.Key.from_path('User', str(uid))
    user = db.get(key)
    if user == None:
        user = User(key_name=str(uid), first_name='-')
        user.put()
    return user

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

        if msg_type in ('daily', 'mass'):
            if msg_type == 'daily':
                user.update_last_auto()

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
    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('RCMealBot backend running...\n')

    def post(self):
        data = json.loads(self.request.body)
        logging.debug(self.request.body)

        msg = data.get('message')
        msg_from = msg.get('from')

        uid = msg_from.get('id')
        first_name = msg_from.get('first_name')
        last_name = msg_from.get('last_name')
        username = msg_from.get('username')

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

            send_message(user, 'Welcome {}! Now you can /login'.format(first_name))

            if new_user:
                send_message(ADMIN_ID, 'New user: ' + user.get_name_string())

            return

        if text == None:
            logging.info(LOG_TYPE_NON_TEXT)
            return

        logging.info(LOG_TYPE_COMMAND + text)

        cmd = text.lower().strip()

        def is_command(word):
            return cmd == '/' + word

        if is_command('login'):
            if user.is_authenticated():
                response = 'You are already logged in as *{}* _({})_. Did you mean to /logout?'.format(user.full_name, user.matric)
                send_message(user, response, markdown=True)
                return

            send_typing(uid)
            jsessionid = get_new_jsessionid()

            if not jsessionid:
                send_message(user, 'Sorry, please try again later')
                return

            url = BASE_URL + 'login.do;jsessionid=' + jsessionid
            response = 'Login here: ' + url + '\n\nWhen done, click /continue'

            user.set_jsessionid(jsessionid)
            send_message(user, response, disable_web_page_preview=True)

        elif is_command('continue'):
            if not user.jsessionid:
                send_message(user, 'Sorry, please /login first')
                return

            send_typing(uid)
            meals = check_meals(user.jsessionid, first_time_user=user)

            if not meals:
                send_message(user, 'Sorry, please try again later')
                return
            elif meals == UNAUTHORISED:
                user.set_authenticated(False)
                response = 'Sorry, that didn\'t work. Please try /login again or, if the problem persists, read on:\n\n'
                response += 'The link must be opened in a fresh browser that has never been used to browse the RC dining portal before. ' + \
                            'Try one of the following:\n'
                response += '- open the link in a new incognito window\n'
                response += '- clear the cookies in your current browser before opening the link\n'
                response += '- open the link with another browser (one you have never used to browse the RC dining portal before)\n'
                send_message(user, response)
                return

            user.set_authenticated(True)
            send_message(user, meals, markdown=True)

        elif is_command('logout'):
            if not user.is_authenticated():
                send_message(user, 'Did you mean to /login?')
                return

            user.set_authenticated(False)
            send_message(user, 'You have successfully logged out. /login again?')

        elif is_command('summary'):
            if not user.is_authenticated():
                send_message(user, 'Please /login first')
                return

            send_typing(uid)
            xls_data = check_meals(user.jsessionid, get_excel=True)
            meals = check_meals(user.jsessionid)

            if not xls_data or not meals:
                send_message(user, 'Sorry, please try again later')
                return
            elif xls_data == UNAUTHORISED or meals == UNAUTHORISED:
                user.set_authenticated(False)
                send_message(user, 'Sorry, your session has expired. Please /login again')
                return

            send_message(user, '*Weekly Summary*\n' + weekly_summary(xls_data) + '\n\n' + meals, markdown=True)

        else:
            if not user.is_authenticated():
                send_message(user, 'Did you mean to /login?')
                return

            send_typing(uid)
            meals = check_meals(user.jsessionid)

            if not meals:
                send_message(user, 'Sorry, please try again later')
                return
            elif meals == UNAUTHORISED:
                user.set_authenticated(False)
                send_message(user, 'Sorry, your session has expired. Please /login again')
                return

            send_message(user, meals, markdown=True)

class SendPage(webapp2.RequestHandler):
    def run(self):
        query = User.all()
        query.filter('active =', True)
        query.filter('last_auto <', get_today_time())

        try:
            for user in query.run(batch_size=500):
                send_message(user, 'devo', msg_type='daily', markdown=True)
        except db.Error as e:
            logging.warning(LOG_ERROR_DATASTORE + str(e))
            return False

        return True

    def get(self):
        if self.run() == False:
            taskqueue.add(url='/send')

    def post(self):
        if self.run() == False:
            self.abort(502)

class AuthPage(webapp2.RequestHandler):
    def run(self):
        query = User.all()
        query.filter('auth =', True)

        try:
            for user in query.run(batch_size=500):
                result = check_auth(user.jsessionid)
                if result:
                    logging.info(LOG_SESSION_ALIVE.format(user.get_name_string()))
                elif result == None:
                    # TODO: enqueue keepalive
                    pass
                else:
                    logging.info(LOG_SESSION_EXPIRED.format(user.get_name_string()))
                    user.set_authenticated(False)
                    send_message(user, 'Sorry, your session has expired. Please /login again')
        except db.Error as e:
            logging.warning(LOG_ERROR_DATASTORE + str(e))
            return False

        return True

    def get(self):
        if self.run() == False:
            taskqueue.add(url='/send')

    def post(self):
        if self.run() == False:
            self.abort(502)

class MigratePage(webapp2.RequestHandler):
    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('Migrate page\n')

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

class MassPage(webapp2.RequestHandler):
    def get(self):
        taskqueue.add(url='/mass')

    def post(self):
        pass

app = webapp2.WSGIApplication([
    ('/', MainPage),
    ('/' + TOKEN, MainPage),
    ('/send', SendPage),
    ('/message', MessagePage),
    ('/auth', AuthPage),
    ('/mass', MassPage),
    ('/migrate', MigratePage),
], debug=True)
