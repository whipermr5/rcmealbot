import webapp2
import logging
import json
import textwrap
import xlrd
import ast
import parsedatetime
from google.appengine.api import urlfetch, taskqueue
from google.appengine.ext import db
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from secrets import TOKEN, ADMIN_ID, APIAI_TOKEN
TELEGRAM_URL = 'https://api.telegram.org/bot' + TOKEN
TELEGRAM_URL_SEND = TELEGRAM_URL + '/sendMessage'
TELEGRAM_URL_CHAT_ACTION = TELEGRAM_URL + '/sendChatAction'
JSON_HEADER = {'Content-Type': 'application/json;charset=utf-8'}
APIAI_URL = 'https://api.api.ai/v1/query?v=20150910'
APIAI_HEADER = {'Content-Type': 'application/json;charset=utf-8',
                'Authorization': 'Bearer ' + APIAI_TOKEN}

BASE_URL = 'https://myaces.nus.edu.sg/Prjhml/'
UNAUTHORISED = 'unauthorised'
SESSION_EXPIRED = 'Sorry {}, your session has expired. Please /login again.'
HEADING_BREAKFAST = u'\U0001F32E' + ' *Breakfast*'
HEADING_DINNER = u'\U0001F35C' + ' *Dinner*'
NOTE_FRUIT = '\n\n' + u'\U0001F34A' + ' _Fruits will be served at the counter_'
NOTE_UNSUBSCRIBE = '\n\n(use /dailyoff to unsubscribe)'
NOTE_UNSUBSCRIBE_WEEKLY = '\n\n(use /weeklyoff to unsubscribe)'
THRESHOLD_VALID_MENU_LENGTH = 50
EMPTY = 'empty'

LOG_SENT = '{} {} sent to uid {} ({})'
LOG_AUTH = 'Authenticating with jsessionid '
LOG_AUTH_FAILED = 'Authentication failed for uid {} ({})'
LOG_AUTH_SUCCESS = 'Successfully authenticated as {} ({})'
LOG_ENQUEUED = 'Enqueued {} to uid {} ({})'
LOG_DID_NOT_SEND = 'Did not send {} to uid {} ({}): {}'
LOG_EMPTY_MEAL_DATA = 'Empty meal data'
LOG_ERROR_SENDING = 'Error sending {} to uid {} ({}):\n{}'
LOG_ERROR_DATASTORE = 'Error reading from datastore:\n'
LOG_ERROR_REMOTE = 'Error accessing site:\n'
LOG_ERROR_AUTH = 'Error sending auth request for uid {} ({})'
LOG_ERROR_QUERY = 'Error querying uid {} ({}): {}'
LOG_ERROR_APIAI_FETCH = 'Error querying api.ai:\n'
LOG_ERROR_APIAI_PARSE = 'Error parsing api.ai response:\n'
LOG_FALLBACK = 'Replying with fallback speech'
LOG_TYPE_START_NEW = 'Type: Start (new user)'
LOG_TYPE_START_EXISTING = 'Type: Start (existing user)'
LOG_TYPE_NON_TEXT = 'Type: Non-text'
LOG_TYPE_NON_MESSAGE = 'Type: Non-message'
LOG_TYPE_EDITED_MESSAGE = 'Type: Edited message'
LOG_TYPE_COMMAND = 'Type: Command\n'
LOG_TYPE_SMALLTALK = 'Type: Small talk'
LOG_USER_MIGRATED = 'User {} migrated to uid {} ({})'
LOG_USER_DELETED = 'Deleted uid {} ({})'
LOG_USER_REACHABLE = 'Uid {} ({}) is still reachable'
LOG_USER_UNREACHABLE = 'Unable to reach uid {} ({}): {}'
LOG_SESSION_ALIVE = 'Session kept alive for {}'
LOG_SESSION_EXPIRED = 'Session expired for {}'
LOG_SESSION_INACTIVE = 'Session inactive for {}'

RECOGNISED_ERROR_PARSE = 'Bad Request: Can\'t parse message text'
RECOGNISED_ERROR_EMPTY = 'Bad Request: Message text is empty'
RECOGNISED_ERROR_MIGRATE = 'Bad Request: group chat is migrated to a supergroup chat'
RECOGNISED_ERRORS = ('PEER_ID_INVALID',
                     'Bot was blocked by the user',
                     'Forbidden: user is deleted',
                     'Forbidden: user is deactivated',
                     'Forbidden: User is deactivated',
                     'Forbidden: bot was blocked by the user',
                     'Forbidden: Bot was blocked by the user',
                     'Forbidden: bot was kicked from the group chat',
                     'Forbidden: bot was kicked from the channel chat',
                     'Forbidden: bot was kicked from the supergroup chat',
                     'Forbidden: bot is not a member of the supergroup chat',
                     'Forbidden: bot can\'t initiate conversation with a user',
                     'Forbidden: Bot can\'t initiate conversation with a user',
                     'Bad Request: chat not found',
                     'Bad Request: PEER_ID_INVALID',
                     'Bad Request: group chat was deactivated',
                     RECOGNISED_ERROR_EMPTY,
                     RECOGNISED_ERROR_MIGRATE)

def get_new_jsessionid():
    url = BASE_URL + 'login.do'

    try:
        result = urlfetch.fetch(url, deadline=10)
    except Exception as e:
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
    except Exception as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None

    return result.status_code == 200

def check_meals(jsessionid, first_time_user=None, get_excel=False):
    url = BASE_URL + 'studstaffMealBalance.do;jsessionid=' + jsessionid
    logging.debug(LOG_AUTH + jsessionid)

    try:
        result = urlfetch.fetch(url, follow_redirects=False, deadline=10)
    except Exception as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None

    if result.status_code != 200:
        return UNAUTHORISED

    html = result.content

    if get_excel:
        start = html.find('<div class="exportlinks"> Export As: ')
        if start == -1:
            return EMPTY
        start += 47
        end = html.find('&amp;', start)
        link = html[start:end]
        xls_url = BASE_URL + link

        try:
            xls_result = urlfetch.fetch(xls_url, follow_redirects=False, deadline=10)
        except Exception as e:
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

        try:
            first_time_user.full_name = BeautifulSoup(full_name, 'lxml').text
            first_time_user.matric = matric
            first_time_user.meal_pref = meal_pref
            first_time_user.put()
        except:
            logging.warning('Error parsing user info: assuming auth failure')
            logging.debug(html)
            return UNAUTHORISED

        logging.info(LOG_AUTH_SUCCESS.format(full_name, matric))
        return 'Success! You are logged in as *{}* _({})_.\n\n'.format(full_name, matric)

    def summarise(html):
        data = ''.join(html.replace('/', '').replace('<td>', ' ')).split()
        d1 = ''
        d2 = ''
        d3 = ''
        d4 = ''
        try:
            d1 = data[1]
            d2 = data[2]
            d3 = data[3]
            d4 = data[5]
        except IndexError:
            logging.info(LOG_EMPTY_MEAL_DATA)
            pass
        return 'Consumed: {}\nForfeited: {}\nCarried forward: {}\nTotal remaining: {}'.format(d1, d2, d3, d4)

    start = html.find('<td class="fieldname" nowrap="true"> Breakfast </td>') + 75
    end = html.find('</tr>', start)
    breakfast = html[start:end]

    start = html.find('<td class="fieldname" nowrap="true"> Dinner </td>') + 72
    end = html.find('</tr>', start)
    dinner = html[start:end]

    return '*Breakfast*\n' + summarise(breakfast) + '\n\n*Dinner*\n' + summarise(dinner)

def weekly_summary(xls_data):
    if xls_data == EMPTY:
        return '0 meals'

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

    try:
        sh = xlrd.open_workbook(file_contents=xls_data).sheet_by_index(0)
    except:
        return ''
    for i in range(1, sh.nrows):
        date = datetime.strptime(sh.row(i)[1].value, '%d/%m/%Y %H:%M:%S')
        week = date.strftime('%Y-W%W')
        this_week = get_today_date().strftime('%Y-W%W')
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

def get_menu(today_date, meal_type, is_auto=False):
    today_date_lookup = today_date.strftime('%Y-%m-%d-')
    if meal_type == 'breakfast':
        menus = ast.literal_eval(get_data().breakfasts)
        today_date_lookup += 'B'
    else:
        menus = ast.literal_eval(get_data().dinners)
        today_date_lookup += 'D'
    notes = ast.literal_eval(get_data().notes)
    cancellations = ast.literal_eval(get_data().cancellations)

    day = (today_date - get_data().start_date).days
    if day < 0 or day >= len(menus):
        return EMPTY

    cancellation = cancellations.get(today_date_lookup)
    if cancellation:
        menu = cancellation
    else:
        menu = menus[day]
        if not menu:
            return None
        if len(menu) > THRESHOLD_VALID_MENU_LENGTH:
            menu += NOTE_FRUIT

    if is_auto:
        menu += NOTE_UNSUBSCRIBE

    note = notes.get(today_date_lookup)
    if note:
        menu += '\f' + note

    friendly_date = today_date.strftime('%-d %b %Y (%A)')
    heading = HEADING_BREAKFAST if meal_type == 'breakfast' else HEADING_DINNER
    return heading + ' - _{}_\n\n'.format(friendly_date) + menu

def get_today_date():
    return (datetime.utcnow() + timedelta(hours=8)).date()

def get_today_time():
    today = get_today_date()
    today_time = datetime(today.year, today.month, today.day) - timedelta(hours=8)
    return today_time

def parse_date(friendly):
    friendly = friendly.decode('utf-8', 'ignore')
    now = datetime.utcnow() + timedelta(hours=8)
    return parsedatetime.Calendar().parseDT(friendly, now)[0].date()

def apiai_post(data, deadline=3, retries=10):
    try:
        output = urlfetch.fetch(url=APIAI_URL, payload=data, method=urlfetch.POST,
                                headers=APIAI_HEADER, deadline=deadline)
    except Exception as e:
        if retries > 0:
            return apiai_post(data, retries=retries - 1)
        else:
            raise e
    return output

def make_smalltalk(query, uid):
    payload = {
        'query': query,
        'sessionId': uid,
        'lang': 'en'
    }
    data = json.dumps(payload)
    try:
        result = apiai_post(data)
    except Exception as e:
        logging.warning(LOG_ERROR_APIAI_FETCH + str(e))
        return None
    try:
        logging.debug(result.content)
        response = json.loads(result.content)
        result = response.get('result')
        action = result.get('action')
        params = result.get('parameters')
        if action in ['breakfast', 'dinner', 'meals']:
            return (action, params, None)
        else:
            speech = result.get('fulfillment').get('speech')
            if not speech:
                speech = 'I\'m a bit confused.'
                logging.info(LOG_FALLBACK)
            logging.info(speech)
            return ('smalltalk', None, speech)
    except Exception as e:
        logging.warning(LOG_ERROR_APIAI_PARSE + str(e))
        return None

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

    def get_description(self):
        user_type = 'group' if self.is_group() else 'user'
        return user_type + ' ' + self.get_name_string()

    def is_group(self):
        return int(self.get_uid()) < 0

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

    def update_last_auto(self, hours=0):
        self.last_auto = get_today_time() + timedelta(hours=hours)
        self.put()

    def update_last_weekly(self):
        self.last_weekly = get_today_time()
        self.put()

    def migrate_to(self, uid):
        props = dict((prop, getattr(self, prop)) for prop in self.properties().keys())
        props.update(key_name=str(uid))
        new_user = User(**props)
        new_user.put()
        self.delete()
        return new_user

class Data(db.Model):
    breakfasts = db.TextProperty()
    dinners = db.TextProperty()
    notes = db.TextProperty()
    cancellations = db.TextProperty()
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

def telegram_post(data, deadline=3):
    return urlfetch.fetch(url=TELEGRAM_URL_SEND, payload=data, method=urlfetch.POST,
                          headers=JSON_HEADER, deadline=deadline)

def telegram_query(uid, deadline=3):
    data = json.dumps({'chat_id': uid, 'action': 'typing'})
    return urlfetch.fetch(url=TELEGRAM_URL_CHAT_ACTION, payload=data, method=urlfetch.POST,
                          headers=JSON_HEADER, deadline=deadline)

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
            logging.info(LOG_ENQUEUED.format(msg_type, uid, user.get_description()))

        if msg_type in ('daily', 'daily2', 'weekly', 'mass'):
            if msg_type == 'daily':
                user.update_last_auto()
            elif msg_type == 'daily2':
                user.update_last_auto(hours=16)
            elif msg_type == 'weekly':
                user.update_last_weekly()

            queue_message()
            return

        try:
            result = telegram_post(data)
        except Exception as e:
            logging.warning(LOG_ERROR_SENDING.format(msg_type, uid, user.get_description(), str(e)))
            queue_message()
            return

        response = json.loads(result.content)
        error_description = str(response.get('description'))

        if error_description.startswith(RECOGNISED_ERROR_PARSE):
            if build.get('parse_mode'):
                del build['parse_mode']
            data = json.dumps(build)
            queue_message()

        elif handle_response(response, user, uid, msg_type) == False:
            queue_message()

    i = 0
    for text in text.split('\f'):
        if len(text) > 4096:
            chunks = textwrap.wrap(text, width=4096, replace_whitespace=False, drop_whitespace=False)
            for chunk in chunks:
                send_short_message(chunk, i)
                i += 1
        else:
            send_short_message(text)

def handle_response(response, user, uid, msg_type):
    if response.get('ok') == True:
        msg_id = str(response.get('result').get('message_id'))
        logging.info(LOG_SENT.format(msg_type.capitalize(), msg_id, uid, user.get_description()))
        user.update_last_sent()

    else:
        error_description = str(response.get('description'))
        if error_description not in RECOGNISED_ERRORS:
            logging.warning(LOG_ERROR_SENDING.format(msg_type, uid, user.get_description(),
                                                     error_description))
            return False

        logging.info(LOG_DID_NOT_SEND.format(msg_type, uid, user.get_description(),
                                             error_description))
        if error_description == RECOGNISED_ERROR_EMPTY:
            return True
        elif error_description == RECOGNISED_ERROR_MIGRATE:
            new_uid = response.get('parameters', {}).get('migrate_to_chat_id')
            if new_uid:
                user = user.migrate_to(new_uid)
                logging.info(LOG_USER_MIGRATED.format(uid, new_uid, user.get_description()))
        else:
            user_description = user.get_description()
            user.delete()
            logging.info(LOG_USER_DELETED.format(uid, user_description))
            return True

        user.set_active(False)
        user.set_active_weekly(False)

    return True

def send_typing(uid):
    data = json.dumps({'chat_id': uid, 'action': 'typing'})
    try:
        rpc = urlfetch.create_rpc()
        urlfetch.make_fetch_call(rpc, url=TELEGRAM_URL_CHAT_ACTION, payload=data,
                                 method=urlfetch.POST, headers=JSON_HEADER)
    except:
        return

class MainPage(webapp2.RequestHandler):
    WELCOME = 'Hello, {}! Welcome! To get started, enter one of the following commands:\n\n'
    HELP = 'Hi {}! Please enter one of the following commands:\n\n'
    ABOUT = 'Created by @whipermr5. Comments, feedback and suggestions are welcome!\n\n' + \
            'Food menu extracted from http://nus.edu.sg/ohs/current-residents/students/dining-daily.php\n\n' + \
            'P.S. CAPT rocks! And God loves you :)'
    UNRESPONSIVE = 'Sorry {}, my logic module isn\'t responding. Talking to humans is hard :( ' + \
                   'Let it rest for awhile and try one of the following dumb commands instead:\n\n'
    REMOTE_ERROR = 'Sorry {}, I\'m having some difficulty accessing the site. ' + \
                   'Please try again later.'

    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('RCMealBot backend running...\n')

    def post(self):
        def build_command_list():
            cmds = '/meals - check meal credits' if user.is_authenticated() else '/login - to check meal credits'
            cmds += '\n/breakfast - view today\'s breakfast menu'
            cmds += '\n/dinner - view today\'s dinner menu'
            cmds += '\n/settings - turn on/off automatic updates'
            cmds += '\n/about - about this bot/ send feedback'
            cmds += '\n/logout' if user.is_authenticated() else ''
            cmds += '\n\n/breakfast (or /dinner) <day> - view the breakfast/dinner menu for a particular day'
            cmds += '\ne.g. /breakfast tomorrow, /breakfast saturday, /dinner next tuesday'
            return cmds

        def build_settings_list():
            cmds = 'Hi, {}!'.format(user.get_first_name())
            if user.is_authenticated():
                cmds += ' You are logged in as *{}* _({})_.'.format(user.full_name, user.matric)
                cmds += ' Weekly meal reports (sent on Sunday nights) are *' + ('on' if user.is_active_weekly() else 'off') + '*.'
            else:
                cmds += ' You are *not* logged in.'
            cmds += ' Daily menu updates (sent at midnight and 4pm) are *' + ('on' if user.is_active() else 'off') + '*.\n\n'
            cmds += '/weeklyoff - turn off weekly meal reports' if user.is_active_weekly() else '/weeklyon - turn on weekly meal reports'
            cmds += '\n/dailyoff - turn off daily menu updates' if user.is_active() else '\n/dailyon - turn on daily menu updates'
            return cmds

        def is_command(word):
            return cmd.startswith('/' + word)

        data = json.loads(self.request.body)
        logging.debug(self.request.body)

        msg = data.get('message')
        if not msg:
            msg = data.get('edited_message')
            if msg:
                logging.info(LOG_TYPE_EDITED_MESSAGE)
            else:
                logging.info(LOG_TYPE_NON_MESSAGE)
                return

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

        def get_from_string():
            name_string = msg_from.get('first_name').encode('utf-8', 'ignore').strip()
            actual_last_name = msg_from.get('last_name')
            actual_username = msg_from.get('username')
            if actual_last_name:
                actual_last_name = actual_last_name.encode('utf-8', 'ignore').strip()
                name_string += ' ' + actual_last_name
            if actual_username:
                actual_username = actual_username.encode('utf-8', 'ignore').strip()
                name_string += ' @' + actual_username
            return name_string

        if user.last_sent == None or text == '/start':
            if user.last_sent == None:
                logging.info(LOG_TYPE_START_NEW)
                new_user = True
            else:
                logging.info(LOG_TYPE_START_EXISTING)
                new_user = False

            send_message(user, self.WELCOME.format(first_name) + build_command_list())

            if new_user:
                if user.is_group():
                    new_alert = 'New group: "{}" via user: {}'.format(first_name, get_from_string())
                else:
                    new_alert = 'New user: ' + get_from_string()
                send_message(ADMIN_ID, new_alert)

            return

        if text == None:
            logging.info(LOG_TYPE_NON_TEXT)
            migrate_to_chat_id = msg.get('migrate_to_chat_id')
            if migrate_to_chat_id:
                new_uid = migrate_to_chat_id
                user = user.migrate_to(new_uid)
                logging.info(LOG_USER_MIGRATED.format(uid, new_uid, user.get_description()))
            return

        if text.startswith('/'):
            logging.info(LOG_TYPE_COMMAND + text)
        else:
            logging.info(LOG_TYPE_SMALLTALK)
            logging.info(text)

        cmd = text.lower().strip()

        def handle_meals():
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

        def handle_menu(meal_type, target_date):
            menu = get_menu(target_date, meal_type=meal_type)
            friendly_date = target_date.strftime('%-d %b %Y (%A)')

            if menu == EMPTY:
                send_message(user, 'Sorry {}, OHS has not uploaded the {} menu for {} yet.'.format(first_name, meal_type, friendly_date))
            elif not menu:
                send_message(user, 'Sorry {}, {} is not served on {}.'.format(first_name, meal_type, friendly_date))
            else:
                send_message(user, menu, markdown=True)

        if is_command('meals'):
            handle_meals()

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
                send_message(user, 'Sorry {}, please /login first.'.format(first_name))
                return

            send_typing(uid)
            welcome = check_meals(user.jsessionid, first_time_user=user)

            if not welcome:
                send_message(user, self.REMOTE_ERROR.format(first_name))
                return
            elif welcome == UNAUTHORISED:
                user.set_authenticated(False)
                logging.info(LOG_AUTH_FAILED.format(uid, user.get_description()))
                response = 'Sorry {}, that didn\'t work. Please try /login again or, if the problem persists, read on:\n\n'.format(first_name)
                response += 'The link must be opened in a fresh browser that has never been used to browse the RC dining portal before. ' + \
                            'Try one of the following:\n'
                response += '- copy and paste the link into a new incognito (Chrome) or private browsing (Safari) window\n'
                response += '- clear the cookies/site data for myaces.nus.edu.sg in your current browser before opening the link:\n'
                response += '-- Chrome app -> browse to myaces.nus.edu.sg -> tap the green lock -> Site Settings -> Clear & Reset\n'
                response += '-- Settings app -> Safari -> Advanced -> Website Data -> Edit -> delete entry for myaces.nus.edu.sg\n'
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

        elif is_command('breakfast'):
            if len(cmd) > 10:
                target_date = parse_date(cmd[10:].strip())
            else:
                target_date = get_today_date()

            handle_menu('breakfast', target_date)

        elif is_command('dinner'):
            if len(cmd) > 7:
                target_date = parse_date(cmd[7:].strip())
            else:
                target_date = get_today_date()

            handle_menu('dinner', target_date)

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
            send_message(user, 'Success! You will receive menu updates every day at midnight and 4pm.')

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
            if user.is_group() or len(text) >= 256:
                return

            send_typing(uid)
            smalltalk = make_smalltalk(text, uid)

            if smalltalk:
                st_type = smalltalk[0]
                st_params = smalltalk[1]
                st_speech = smalltalk[2]
                if st_type in ['breakfast', 'dinner']:
                    if st_params.get('date'):
                        target_date = parse_date(st_params.get('date'))
                    else:
                        target_date = get_today_date()
                    handle_menu(st_type, target_date)
                elif st_type == 'meals':
                    handle_meals()
                else:
                    send_message(user, st_speech)
            else:
                send_message(user, self.UNRESPONSIVE.format(first_name) + build_command_list())

class DailyPage(webapp2.RequestHandler):
    def run(self, meal_type):
        menu = get_menu(get_today_date(), meal_type=meal_type, is_auto=True)
        if not menu or menu == EMPTY:
            return True

        if meal_type == 'breakfast':
            msg_type = 'daily'
            hours = 0
        else:
            msg_type = 'daily2'
            hours = 16

        expected_time_after_update = get_today_time() + timedelta(hours=hours)

        query = User.all()
        query.filter('active =', True)
        query.filter('last_auto <', expected_time_after_update)

        try:
            for user in query.run(batch_size=500):
                send_message(user, menu, msg_type=msg_type, markdown=True)
        except Exception as e:
            logging.warning(LOG_ERROR_DATASTORE + str(e))
            return False

        return True

    def get(self):
        meal_type = self.request.get('meal_type', 'breakfast')
        if self.run(meal_type) == False:
            taskqueue.add(url='/daily', payload=meal_type)

    def post(self):
        meal_type = self.request.body
        if self.run(meal_type) == False:
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

                summary = '*Weekly Summary*\nYou had ' + weekly_summary(xls_data) + ' this week.\n\n' + meals + NOTE_UNSUBSCRIBE_WEEKLY
                send_message(user, summary, msg_type='weekly', markdown=True)
        except Exception as e:
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
        except Exception as e:
            logging.warning(LOG_ERROR_SENDING.format(msg_type, uid, user.get_description(), str(e)))
            logging.debug(data)
            self.abort(502)

        response = json.loads(result.content)

        if handle_response(response, user, uid, msg_type) == False:
            logging.debug(data)
            self.abort(502)

class AuthPage(webapp2.RequestHandler):
    def run(self):
        query = User.all(keys_only=True)
        query.filter('auth =', True)

        try:
            for key in query.run(batch_size=500):
                uid = key.name()
                taskqueue.add(queue_name='reauth', url='/reauth', payload=uid)
        except Exception as e:
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
        if not user.jsessionid:
            logging.info(LOG_SESSION_INACTIVE.format(user.get_description()))
            user.set_authenticated(False)
            return

        result = check_auth(user.jsessionid)
        if result:
            logging.info(LOG_SESSION_ALIVE.format(user.get_description()))
        elif result == None:
            logging.warning(LOG_ERROR_AUTH.format(user.get_uid(), user.get_description()))
            self.abort(502)
        else:
            logging.info(LOG_SESSION_EXPIRED.format(user.get_description()))
            user.set_authenticated(False)
            send_message(user, SESSION_EXPIRED.format(user.get_first_name()))

class MenuPage(webapp2.RequestHandler):
    def get(self):
        if self.request.get('commit', False):
            commit = True
        else:
            commit = False

        url_format = 'http://hg.sg/nus_ohs_admin/adminOHS/backend/script/index.php?' + \
                     'controller=pjFront&action=pjActionLoadEventDetail&index=4455&cate=0&dt={}'
        start_date = datetime(2017, 1, 8).date()

        def get_category(soup):
            html = str(soup)
            if 'helpyourself.png' in html:
                return 'Help Yourself'
            elif 'western.png' in html:
                return 'Western'
            elif 'timsum.png' in html:
                return 'Tim Sum'
            elif 'asian.png' in html:
                return 'Asian'
            elif 'veg.png' in html:
                return 'Vegetarian'
            elif 'muslim.png' in html:
                return 'Malay (Halal)'
            elif 'grab.png' in html:
                return 'Grab & Go'
            elif 'indian.png' in html:
                return 'Indian'
            elif 'noodle.png' in html:
                return 'Noodle'
            elif 'specials.png' in html:
                return 'Special of the Day'
            else:
                return soup.text.strip().title()

        def get_text(soup):
            for tag in soup.select('br'):
                tag.name = 'span'
                tag.string = '\n'
            output = ''
            for line in soup.text.split('\n'):
                output += line.strip() + '\n'
            return output.strip()

        def get_menu(soup):
            try:
                output = ''
                for tr in soup.select('tr'):
                    tds = tr.select('td')
                    category = get_category(tds[0])
                    text = get_text(tds[1])
                    if not category and not text:
                        continue
                    output += '*~ {} ~*\n'.format(category) + text + '\n\n'
                return output.rstrip()
            except:
                return soup.text.strip()

        def get_menus(url):
            try:
                result = urlfetch.fetch(url, deadline=10)
            except Exception as e:
                logging.warning(LOG_ERROR_REMOTE + str(e))
                self.abort(502)
            html = result.content
            soup = BeautifulSoup(html, 'lxml')
            headers = soup.select('.pull-left')
            bodies = soup.select('.tbl-menuu')
            while len(headers) < len(bodies):
                bodies.pop(0)
            while len(bodies) < len(headers):
                headers.pop(0)
            if len(headers) == 0:
                return (None, None)
            elif len(headers) == 1:
                header = headers[0].text.lower()
                if 'breakfast' in header:
                    return (get_menu(bodies[0]), None)
                else:
                    return (None, get_menu(bodies[0]))
            else:
                breakfast = None
                dinner = None
                for i in range(len(headers)):
                    header = headers[i].text.lower()
                    if 'breakfast' in header and not breakfast:
                        breakfast = get_menu(bodies[i])
                    elif 'dinner' in header and not dinner:
                        dinner = get_menu(bodies[i])

                return (breakfast, dinner)

        breakfasts = []
        dinners = []
        days = 0
        while True:
            url = url_format.format((start_date + timedelta(days=days)).strftime('%Y-%m-%d'))
            days += 1
            result = get_menus(url)
            if result == (None, None):
                break
            breakfast = result[0]
            dinner = result[1]
            breakfasts.append(breakfast)
            dinners.append(dinner)

        days = len(breakfasts)
        data = get_data()
        if commit:
            data.breakfasts = str(breakfasts)
            data.dinners = str(dinners)
            data.start_date = start_date
            data.put()
            start_date_text = start_date.strftime('%d %b %Y')
            logging.info('Updated menu from {} for {} days'.format(start_date_text, days))
        else:
            changed = str(breakfasts) != data.breakfasts or str(dinners) != data.dinners
            if changed:
                logging.info('Detected change in menu')
                send_message(ADMIN_ID, 'OHS has updated the menu!')
            else:
                logging.info('No change in menu detected')

class MigratePage(webapp2.RequestHandler):
    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('Migrate page\n')
        # data = get_data()
        # new_data = Data(key_name='main_1617s1')
        # new_data.breakfasts = data.breakfasts
        # new_data.dinners = data.dinners
        # new_data.start_date = data.start_date
        # new_data.notes = data.notes
        # new_data.cancellations = data.cancellations
        # new_data.put()

class MassPage(webapp2.RequestHandler):
    def get(self):
        taskqueue.add(url='/mass')

    def post(self):
        # try:
        #     # one_hour_ago = datetime.now() - timedelta(hours=1)
        #     query = User.all()
        #     # query.filter('created <', one_hour_ago)
        #     for user in query.run(batch_size=500):
        #         mass_msg = '*Update*\n\nThank you for using @rcmealbot and for giving valuable feedback! ' + \
        #         'Here are some *improvements* to the bot:\n\n' + \
        #         u'\U0001F539' + ' /breakfast and /dinner menus are now *separate*\n' + \
        #         u'\U0001F539' + ' breakfast updates will be sent at midnight while dinner updates will be sent at *4pm* daily\n' + \
        #         u'\U0001F539' + ' command to check credits has been *simplified* to /meals\n\n' + \
        #         'Examples:\n/breakfast tomorrow\n/dinner next monday\n/meals\n\n' + \
        #         'Hope you\'ll continue to find the bot useful! :)\n\n- rcmealbot admin'
        #         send_message(user, mass_msg, msg_type='mass', markdown=True)

        # except Exception as e:
        #     logging.error(e)
        pass

class VerifyPage(webapp2.RequestHandler):
    def get(self):
        try:
            query = User.all()
            for user in query.run(batch_size=3000):
                uid = str(user.get_uid())
                taskqueue.add(url='/verify', payload=uid)
            self.response.headers['Content-Type'] = 'text/plain'
            self.response.write('Cleanup in progress\n')
        except Exception as e:
            logging.error(e)

    def post(self):
        uid = self.request.body
        user = get_user(uid)

        try:
            result = telegram_query(uid, 4)
        except Exception as e:
            logging.warning(LOG_ERROR_QUERY.format(uid, user.get_description(), str(e)))
            self.abort(502)

        response = json.loads(result.content)
        if response.get('ok') == True:
            logging.info(LOG_USER_REACHABLE.format(uid, user.get_description()))
        else:
            error_description = str(response.get('description'))
            if error_description == RECOGNISED_ERROR_MIGRATE:
                new_uid = response.get('parameters', {}).get('migrate_to_chat_id')
                if new_uid:
                    user = user.migrate_to(new_uid)
                    logging.info(LOG_USER_MIGRATED.format(uid, new_uid, user.get_description()))
            elif error_description in RECOGNISED_ERRORS:
                user_description = user.get_description()
                user.delete()
                logging.info(LOG_USER_DELETED.format(uid, user_description))
            else:
                logging.warning(LOG_USER_UNREACHABLE.format(uid, user.get_description(), error_description))
                self.abort(502)

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
    ('/verify', VerifyPage),
], debug=True)
