import webapp2
import logging
import json
import textwrap
import xlrd
import ast
import parsedatetime
from google.appengine.api import urlfetch, taskqueue
from google.appengine.ext import ndb
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from secrets import TOKEN, ADMIN_ID, APIAI_TOKEN

# Setting up Bot's common URLs
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

#getting jsessionid (after logging in) from meal plan portal
def get_new_jsessionid():
    url = BASE_URL + 'login.do' #the login url
    try:
        result = urlfetch.fetch(url, deadline=10) #try to go to url and get a response
    except Exception as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None

    html = result.content
    idx = html.find('jsessionid=') + 11 #finding the start of the index of the jsessionid value
    jsessionid = html[idx:idx+68] #68 is the length of the jsessionid
    return jsessionid

#uses the user's jesssionid to see it can fetch meal balance
def check_auth(user):
    url = BASE_URL + 'studstaffMealBalance.do;jsessionid=' + user.jsessionid
    logging.debug(LOG_AUTH + user.jsessionid)

    try:
        result = urlfetch.fetch(url, method=urlfetch.HEAD, follow_redirects=False, deadline=10)
        user.inc_jsessionid() #calls jesssionid
    except Exception as e:
        logging.warning(LOG_ERROR_REMOTE + str(e))
        return None #NOT SUCCESSFUL

    return result.status_code == 200 #SUCCESS

def check_meals(user, first_time_user=False, get_excel=False):
    url = BASE_URL + 'studstaffMealBalance.do;jsessionid=' + user.jsessionid
    logging.debug(LOG_AUTH + user.jsessionid)

    try:
        result = urlfetch.fetch(url, follow_redirects=False, deadline=10)
        user.inc_jsessionid()
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
            user.inc_jsessionid()
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
            user.full_name = BeautifulSoup(full_name, 'lxml').text
            user.matric = matric
            user.meal_pref = meal_pref
            user.put()
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

#Get
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

def get_today_date(): #getting date corrected to SGT
    return (datetime.utcnow() + timedelta(hours=8)).date()

def get_today_time(): #getting time corrected to SGT
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

#User Data Model
class User(ndb.Model):
    username = ndb.StringProperty(indexed=False)
    first_name = ndb.TextProperty()
    last_name = ndb.TextProperty()

    created = ndb.DateTimeProperty(auto_now_add=True)
    last_received = ndb.DateTimeProperty(auto_now=True, indexed=False)

    last_sent = ndb.DateTimeProperty(indexed=False)

    last_auto = ndb.DateTimeProperty(auto_now_add=True)
    last_weekly = ndb.DateTimeProperty(auto_now_add=True)
    active = ndb.BooleanProperty(default=True)
    active_weekly = ndb.BooleanProperty(default=True)

    jsessionid = ndb.StringProperty(indexed=False)
    auth = ndb.BooleanProperty(default=False)

    full_name = ndb.StringProperty(indexed=False)
    matric = ndb.StringProperty(indexed=False)
    meal_pref = ndb.StringProperty(indexed=False)

    def get_uid(self): #gets the id of the user's key. if it's a group (no numeric id for a user, since there isn't one user), it returns None
        return self.key.id()

    def get_first_name(self): #gets a first name in utf-8 format (encode) string without whitespaces before and after (strip)
        return self.first_name.encode('utf-8', 'ignore').strip()

    def get_name_string(self): #gets a <first name> <lastname> @<username> in utf-8 format (encode) string without whitespaces before and after (string)
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

    def is_group(self): #checks if the bot is in a group. if it's a group, it will return True since None < 0 in python.
        return int(self.get_uid()) < 0

    def is_active(self): #checks for user.active 's value
        return self.active

    def is_active_weekly(self): #checks for user.active_weekly 's value
        return self.active_weekly

    def is_authenticated(self): #checks for user.auth 's value
        return self.auth

    def set_active(self, active): #simple setting of active. Saves the setting to database afterwards
        self.active = active
        self.put()

    def set_active_weekly(self, active_weekly): #simple setting of active_weekly. Saves the setting to database afterwards
        self.active_weekly = active_weekly
        self.put()

    def set_authenticated(self, auth): #simple setting of auth to true/false. if false, clear jsessionid. Saves to database afterwards
        self.auth = auth
        if not auth:
            self.jsessionid = None
        self.put()

    def set_jsessionid(self, jsessionid): #simple setting of jsessionid and saving the database
        self.jsessionid = jsessionid
        self.put()

    def inc_jsessionid(self): #mutates jsessionid and resaves it so that
        prev_ver = int(self.jsessionid[67:])
        new_jsessionid = self.jsessionid[:67] + str(prev_ver + 1)
        self.set_jsessionid(new_jsessionid)

    def update_last_sent(self): #updates last_sent to current time, and then saves it to database
        self.last_sent = datetime.now()
        self.put()

    def update_last_auto(self, hours=0): #update last_auto to corrected date_time + timedelta in hours, and then saves it to database.
        self.last_auto = get_today_time() + timedelta(hours=hours)
        self.put()

    def update_last_weekly(self): #update last_weekly to corrected date_time, and then saves it to database
        self.last_weekly = get_today_time()
        self.put()

    def migrate_to(self, uid):
        props = dict((prop, getattr(self, prop)) for prop in self._properties.keys()) #constructs a dictionary of its own properties.
        new_user = User(id=str(uid)) #make a new instance of user with uid. this new instance will be the the target to migrate the data to
        new_user.populate(**props) #populates the new user instance with the props dictionary.
        new_user.put() #saves the new user
        self.key.delete() #deletes THIS user (since its data has already been migrated)
        return new_user #returns the new user instance

#MEAL data model
class Data(ndb.Model):
    breakfasts = ndb.TextProperty()
    dinners = ndb.TextProperty()
    notes = ndb.TextProperty(default='{}')
    cancellations = ndb.TextProperty(default='{}')
    start_date = ndb.DateProperty(indexed=False)

def get_user(uid): #get user based on uid, if user doesn't exist, a new user instance is created with uid and saved.
    user = ndb.Key('User', str(uid)).get()
    if user is None:
        user = User(id=str(uid), first_name='-')
        user.put()
    return user


def get_data(): # DATA ??? what data?
    data = ndb.Key('Data', 'main').get()
    if data is None:
        data = Data(id='main')
        data.put()
    return data


def update_profile(uid, uname, fname, lname): #updates profile
    user = get_user(uid)
    user.username = uname
    user.first_name = fname
    user.last_name = lname
    user.put()
    return user

#sends a sendMessage to bot so that it sends a message
def telegram_post(data, deadline=3):
    return urlfetch.fetch(url=TELEGRAM_URL_SEND, payload=data, method=urlfetch.POST,
                          headers=JSON_HEADER, deadline=deadline)

#sending a chat action to show that something (in this case, a query) is loading. More of a UI manipulation than anything???
def telegram_query(uid, deadline=3):
    data = json.dumps({'chat_id': uid, 'action': 'typing'})
    return urlfetch.fetch(url=TELEGRAM_URL_CHAT_ACTION, payload=data, method=urlfetch.POST,
                          headers=JSON_HEADER, deadline=deadline)

#builds up a data file from the arguments and sends it as an argument to telegram_post for the bot to send a message to the user
def send_message(user_or_uid, text, msg_type='message', force_reply=False, markdown=False, disable_web_page_preview=True):
    try: #GETTING USER ID from user, setting user to input user
        uid = str(user_or_uid.get_uid())
        user = user_or_uid
    except AttributeError: # getting user FROM user_id, setting user to queried user
        uid = str(user_or_uid)
        user = get_user(user_or_uid)

    def send_short_message(text, countdown=0):
        build = {
            'chat_id': uid,
            'text': text
        }
        #setting parameters force_reply, markdown, disable_web_page_preview for telegram_post.
        if force_reply:
            build['reply_markup'] = {'force_reply': True}
        if markdown:
            build['parse_mode'] = 'Markdown'
        if disable_web_page_preview:
            build['disable_web_page_preview'] = True
        #Serializing data so far into json.
        data = json.dumps(build)

        #method to queue message
        def queue_message():
            payload = json.dumps({
                'msg_type': msg_type,
                'data': data
            }) #construction of payload with msg_type and data
            taskqueue.add(url='/message', payload=payload, countdown=countdown)
            logging.info(LOG_ENQUEUED.format(msg_type, uid, user.get_description()))

        if msg_type in ('daily', 'daily2', 'weekly', 'mass'): #checking if message is one of these types
            if msg_type == 'daily': #if it's daily (breakfast alert), call update_last_auto()
                user.update_last_auto()
            elif msg_type == 'daily2': # if it's daily2 (dinner alert), call update_last_auto(hours=16)
                user.update_last_auto(hours=16)
            elif msg_type == 'weekly': #if it's weekly (meal credits alert), call update_last_weekly()
                user.update_last_weekly()

            queue_message() #proceeds to queue message
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

        elif handle_response(response, user, uid, msg_type) == False: #IF RESPONSE gives false, we proceed to try to queue message again
            queue_message()

    #sending a long message as multiple short messages
    i = 0
    for text in text.split('\f'):
        if len(text) > 4096:
            chunks = textwrap.wrap(text, width=4096, replace_whitespace=False, drop_whitespace=False)
            for chunk in chunks:
                send_short_message(chunk, i)
                i += 1
        else:
            send_short_message(text)


def handle_response(response, user, uid, msg_type): #process the response of a POST request from bot
    if response.get('ok') == True: #message sent successfully!
        msg_id = str(response.get('result').get('message_id'))
        logging.info(LOG_SENT.format(msg_type.capitalize(), msg_id, uid, user.get_description()))
        user.update_last_sent() #update what user was last sent a message

    else:
        error_description = str(response.get('description'))
        if error_description not in RECOGNISED_ERRORS:
            logging.warning(LOG_ERROR_SENDING.format(msg_type, uid, user.get_description(),
                                                     error_description))
            return False

        logging.info(LOG_DID_NOT_SEND.format(msg_type, uid, user.get_description(),
                                             error_description))
        #PREDICTABLE ERROR HANDLING
        if error_description == RECOGNISED_ERROR_EMPTY:
            return True
        elif error_description == RECOGNISED_ERROR_MIGRATE:
            new_uid = response.get('parameters', {}).get('migrate_to_chat_id')
            if new_uid:
                user = user.migrate_to(new_uid)
                logging.info(LOG_USER_MIGRATED.format(uid, new_uid, user.get_description()))
        else:
            user_description = user.get_description()
            user.key.delete()
            logging.info(LOG_USER_DELETED.format(uid, user_description))
            return True

        user.set_active(False)
        user.set_active_weekly(False)

    return True

##uses sendChatAction to show that the bot is working on a response (a circular loading thingy??)
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
            'Food menu extracted from http://nus.edu.sg/ohs/current-residents/students/dining-daily.php\n\n'
    UNRESPONSIVE = 'Sorry {}, my logic module isn\'t responding. Talking to humans is hard :( ' + \
                   'Let it rest for awhile and try one of the following dumb commands instead:\n\n' + \
                   'P.S. CAPT rocks! And God loves you :)'
    REMOTE_ERROR = 'Sorry {}, I\'m having some difficulty accessing the site. ' + \
                   'Please try again later.'

    #How the mainpage (i.e. "<url>/") responds to GET requests
    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('RCMealBot backend running...\n')

    #How the mainpage (i.e. "<url>/") responds to POST requests
    def post(self):
        #Building a list of usable commands (if/else statements to show commands for users)
        #Just a command list to present to users, nothing binding.
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

        #Building a settings view when one calls the /settings command
        def build_settings_list():
            cmds = 'Hi, {}!'.format(user.get_first_name()) #Hi, Chatter!
            if user.is_authenticated():
                cmds += ' You are logged in as *{}* _({})_.'.format(user.full_name, user.matric) #You are logged in as *Chatter Batter, A9999999Z*
                cmds += ' Weekly meal reports (sent on Sunday nights) are *' + ('on' if user.is_active_weekly() else 'off') + '*.'
            else:
                cmds += ' You are *not* logged in.'
            cmds += ' Daily menu updates (sent at midnight and 4pm) are *' + ('on' if user.is_active() else 'off') + '*.\n\n' #Daily menu updates (sent at midnight and 4pm) are *on*.
            cmds += '/weeklyoff - turn off weekly meal reports' if user.is_active_weekly() else '/weeklyon - turn on weekly meal reports'
            cmds += '\n/dailyoff - turn off daily menu updates' if user.is_active() else '\n/dailyon - turn on daily menu updates'
            return cmds

        #responds to commands???
        def is_command(word):
            return cmd.startswith('/' + word)

        data = json.loads(self.request.body) #deserializing json in POST request body
        logging.debug(self.request.body) # prints debug message

        msg = data.get('message') #getting the msg out of the message field of data
        if not msg:
            msg = data.get('edited_message') #from edited_message field
            if msg:
                logging.info(LOG_TYPE_EDITED_MESSAGE) #prints debug message with INFO: pretext
            else:
                logging.info(LOG_TYPE_NON_MESSAGE)
                return

        msg_chat = msg.get('chat') #chat field of msg
        msg_from = msg.get('from') #"from" field of msg

        if msg_chat.get('type') == 'private': #handle private messages??
            uid = msg_from.get('id')
            first_name = msg_from.get('first_name')
            last_name = msg_from.get('last_name')
            username = msg_from.get('username')
        else:
            uid = msg_chat.get('id')
            first_name = msg_chat.get('title')
            last_name = None
            username = None

        user = update_profile(uid, username, first_name, last_name) #calls update_profile (L452) with the required arguments

        first_name = first_name.encode('utf-8', 'ignore').strip() # encodes, and leaves out any unallowed characters, then strips white space from the beginning and the end of the string
        if username:
            username = username.encode('utf-8', 'ignore').strip()
        if last_name:
            last_name = last_name.encode('utf-8', 'ignore').strip()
        text = msg.get('text') # get from "text" field
        if text:
            text = text.encode('utf-8', 'ignore')

        def get_from_string(): #gives name string (<first_name> <actual_last_name>/@<actual username>)
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

        #CHECK user has ever sent a user or that a /start command is given.
        if user.last_sent is None or text == '/start':
            if user.last_sent is None:
                logging.info(LOG_TYPE_START_NEW) #is a new user
                new_user = True
            else:
                logging.info(LOG_TYPE_START_EXISTING) #not a new user
                new_user = False

            #sends welcome message with name and list of commands
            send_message(user, self.WELCOME.format(first_name) + build_command_list())

            #for new users
            if new_user:
                if user.is_group(): #for group convos
                    new_alert = 'New group: "{}" via user: {}'.format(first_name, get_from_string())
                else: #for private convos
                    new_alert = 'New user: ' + get_from_string()
                send_message(ADMIN_ID, new_alert) #sends message to admin???

            return

        if text is None:
            logging.info(LOG_TYPE_NON_TEXT)
            migrate_to_chat_id = msg.get('migrate_to_chat_id') ## what this???
            if migrate_to_chat_id:
                new_uid = migrate_to_chat_id
                user = user.migrate_to(new_uid)
                logging.info(LOG_USER_MIGRATED.format(uid, new_uid, user.get_description()))
            return

        if text.startswith('/'): #logging commands
            logging.info(LOG_TYPE_COMMAND + text)
        else:
            logging.info(LOG_TYPE_SMALLTALK)
            logging.info(text)

        cmd = text.lower().strip() #lower case all the command characters

        def handle_meals(): #probably to show meal credits and stuff??
            if not user.is_authenticated():
                send_message(user, 'Did you mean to /login?') #prompt login
                return

            send_typing(uid) #calls on send_typing (L573)
            xls_data = check_meals(user, get_excel=True) #what excel sheet??? meals? check_meals grabs user data from api
            meals = check_meals(user) #calls on check_meals

            if not xls_data or not meals:
                send_message(user, self.REMOTE_ERROR.format(first_name)) ## send error message to user since not logged in
                return
            elif xls_data == UNAUTHORISED or meals == UNAUTHORISED:
                user.set_authenticated(False) ## unauthorized, session invalid. maybe session expired
                send_message(user, SESSION_EXPIRED.format(first_name))
                return

            send_message(user, 'You\'ve had ' + weekly_summary(xls_data) + ' this week.\n\n' + meals, markdown=True)

        #SHOWING MENU with handle_menu(breakfast/dinner, date)
        def handle_menu(meal_type, target_date):
            menu = get_menu(target_date, meal_type=meal_type) #calls on get_menu
            friendly_date = target_date.strftime('%-d %b %Y (%A)') #formats into readable/sensible string

            if menu == EMPTY:
                send_message(user, 'Sorry {}, OHS has not uploaded the {} menu for {} yet.'.format(first_name, meal_type, friendly_date))
            elif not menu:
                send_message(user, 'Sorry {}, {} is not served on {}.'.format(first_name, meal_type, friendly_date))
            else:
                send_message(user, menu, markdown=True)

        #handling meal commands
        if is_command('meals'):
            handle_meals()

        #handling login commands
        elif is_command('login'):
            #user is authenticated
            if user.is_authenticated():
                response = 'You are already logged in as *{}* _({})_. Did you mean to /logout?'.format(user.full_name, user.matric)
                send_message(user, response, markdown=True)
                return

            send_typing(uid)
            jsessionid = get_new_jsessionid()

            #session expired
            if not jsessionid:
                send_message(user, self.REMOTE_ERROR.format(first_name))
                return

            url = BASE_URL + 'login.do;jsessionid=' + jsessionid
            response = 'Login here: ' + url + '\n\nAfter logging in, close the page (be sure not to click on any links), come back here and type /continue'

            user.set_jsessionid(jsessionid[:67] + '1')
            send_message(user, response)

        #handling continue commands
        elif is_command('continue'):
            if not user.jsessionid:
                send_message(user, 'Sorry {}, please /login first.'.format(first_name))
                return

            send_typing(uid)
            welcome = check_meals(user, first_time_user=True)

            if not welcome:
                send_message(user, self.REMOTE_ERROR.format(first_name))
                return
            elif welcome == UNAUTHORISED: #failed login/yet to login
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
            xls_data = check_meals(user, get_excel=True)
            meals = check_meals(user)

            if not xls_data or not meals or xls_data == UNAUTHORISED or meals == UNAUTHORISED:
                return

            send_message(user, 'You\'ve had ' + weekly_summary(xls_data) + ' this week.\n\n' + meals, markdown=True)

        #handling /breakfast command
        elif is_command('breakfast'):
            if len(cmd) > 10:
                target_date = parse_date(cmd[10:].strip())
            else:
                target_date = get_today_date()

            handle_menu('breakfast', target_date)
        #handling/dinner command
        elif is_command('dinner'):
            if len(cmd) > 7:
                target_date = parse_date(cmd[7:].strip())
            else:
                target_date = get_today_date()

            handle_menu('dinner', target_date)
        #handling /settings command
        elif is_command('settings'):
            send_message(user, build_settings_list(), markdown=True)

        #handling weeklyoff command
        elif is_command('weeklyoff'):
            if not user.is_active_weekly():
                send_message(user, 'Weekly meal reports are already off.')
                return

            user.set_active_weekly(False)
            send_message(user, 'Success! You will no longer receive weekly meal reports.')
        #handling weeklyon command
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

        #handle logging out
        elif is_command('logout'):
            #unauthenticated (no need for anything)
            if not user.is_authenticated():
                send_message(user, 'Did you mean to /login?')
                return

            user.set_authenticated(False)
            send_message(user, 'You have successfully logged out. /login again?')

        #invalid/dummy commands
        else:
            if user.is_group() or len(text) >= 256: #IGNORE if it's in a group or lengt of command too long
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

#sends daily menus????
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

        query = User.query(User.active == True, User.last_auto < expected_time_after_update)

        #try to send messages to users in batch of 500? catches an exception if datastore runs out or something?
        try:
            for user in query.iter(batch_size=500):
                send_message(user, menu, msg_type=msg_type, markdown=True)
        except Exception as e:
            logging.warning(LOG_ERROR_DATASTORE + str(e))
            return False

        return True

    def get(self):
        meal_type = self.request.get('meal_type', 'breakfast') # what's this for...?
        if self.run(meal_type) == False: #if run returns false, then add this get request to the queue)
            taskqueue.add(url='/daily', payload=meal_type)

    def post(self): #SELF CALLED. does not get added to queue if the run fails. Basically ignored.
        meal_type = self.request.body
        if self.run(meal_type) == False:
            self.abort(502)

#SENDS weekly reports (MEAL CREDITS etc)
class WeeklyPage(webapp2.RequestHandler):
    def run(self):
        query = User.query(User.auth == True, User.active_weekly == True, User.last_weekly < get_today_time())

        try:
            for user in query.iter(batch_size=500):

                xls_data = check_meals(user, get_excel=True)
                meals = check_meals(user)

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
        params = json.loads(self.request.body) #deserializes json into dictionary
        msg_type = params.get('msg_type')
        data = params.get('data')
        uid = str(json.loads(data).get('chat_id')) #gets value of chat_id field from data json and converts it to string to be uid
        user = get_user(uid) #calls on get_user with uid

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
        query = User.query(User.auth == True)

        try:
            for key in query.iter(batch_size=500, keys_only=True):
                uid = key.id()
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

        result = check_auth(user)
        if result:
            logging.info(LOG_SESSION_ALIVE.format(user.get_description()))
        elif result is None:
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

        data = get_data()
        start_date = data.start_date
        start_date_text = start_date.strftime('%d %b %Y')
        logging.info('Starting menu update from {}'.format(start_date_text))

        def detect_category(img_src):
            if 'helpyourself.png' in img_src:
                return 'Help Yourself'
            elif 'western.png' in img_src:
                return 'Western'
            elif 'timsum.png' in img_src:
                return 'Tim Sum'
            elif 'asian.png' in img_src:
                return 'Asian'
            elif 'veg.png' in img_src:
                return 'Vegetarian'
            elif 'muslim.png' in img_src:
                return 'Malay (Halal)'
            elif 'grab.png' in img_src:
                return 'Grab & Go'
            elif 'indian.png' in img_src:
                return 'Indian'
            elif 'noodle.png' in img_src:
                return 'Noodle'
            elif 'specials.png' in img_src:
                return 'Special of the Day'
            elif 'extra.png' in img_src:
                return 'Extra'
            else:
                return None

        def get_categories(category_img_data):
            categories = []
            for category_img in category_img_data.select('img'):
                detected_category = detect_category(category_img.get('src'))
                if detected_category:
                    categories.append(detected_category)
            if not categories:
                return category_img_data.text.strip().title()
            categories_set = sorted(set(categories), key=categories.index)
            return ' / '.join(categories_set)

        def get_text(menu_items):
            return '\n'.join([menu_item.text.strip() for menu_item in menu_items.select('td')])

        def get_menu(day_menu):
            try:
                output = ''
                for category_row in day_menu.select('> tbody > tr'):
                    category_data = category_row.select('td')
                    category = get_categories(category_data[0])
                    text = get_text(category_data[1])
                    if not category and not text:
                        continue
                    output += '*~ {} ~*\n'.format(category) + text + '\n\n'
                return output.rstrip()
            except:
                return day_menu.text.strip()

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
        if commit:
            data.breakfasts = str(breakfasts)
            data.dinners = str(dinners)
            data.put()
            logging.info('Updated menu from {} for {} days'.format(start_date_text, days))
        else:
            changed = str(breakfasts) != data.breakfasts or str(dinners) != data.dinners
            if changed:
                logging.info('Detected change in menu')
                send_message(ADMIN_ID, 'OHS has updated the menu!')
            else:
                logging.info('No change in menu detected')

#does nothing actually
class MigratePage(webapp2.RequestHandler):
    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('Migrate page\n')
        # data = get_data()
        # new_data = Data(id='main_1617s2')
        # new_data.breakfasts = data.breakfasts
        # new_data.dinners = data.dinners
        # new_data.start_date = data.start_date
        # new_data.notes = data.notes
        # new_data.cancellations = data.cancellations
        # new_data.put()


#sending a message to the users??
class MassPage(webapp2.RequestHandler):
    def get(self):
        taskqueue.add(url='/mass')

    def post(self):
        # try:
        #     # one_hour_ago = datetime.now() - timedelta(hours=1)
        #     query = User.query()
        #     # query = query.filter(User.created < one_hour_ago)
        #     for user in query.iter(batch_size=500):
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

#used to verify users?
class VerifyPage(webapp2.RequestHandler):
    def get(self):
        try:
            query = User.query() #GET ALL USERS
            for user in query.iter(batch_size=3000): #iterates in batches of 3000 (higher the batch size, the more memory it uses. but lesser RPC(Remote Procedure Call Framework) calls)
                uid = str(user.get_uid()) #gets the user's userid
                taskqueue.add(url='/verify', payload=uid) #queues up a post request back to this verify page
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
                user.key.delete()
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
    ('/migrate', MigratePage), #non functional
    ('/mass', MassPage), #non functional (mostly commented out)
    ('/menu', MenuPage),
    ('/verify', VerifyPage),
], debug=True)
