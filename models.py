#!/usr/bin/python
# -*- coding: utf8 -*-

from datetime import datetime, timedelta, time
from google.appengine.ext import ndb
from google.appengine.api import mail, search
from constants import EVENT, USER, TASK, READABLE, JOURNALTAG, REPORT
import tools
import json
import random
import logging
import re
import imp
import hashlib
from common.decorators import auto_cache
try:
    imp.find_module('secrets', ['settings'])
except ImportError:
    from settings import secrets_template as secrets
else:
    from settings import secrets


class UserAccessible(ndb.Model):
    '''
    Parent class for items that have gated user access
    All UserAccessible models should have parent = owning user
    '''

    @classmethod
    def GetAccessible(cls, key_or_id, user, urlencoded_key=False):
        if key_or_id:
            if not urlencoded_key:
                key = ndb.Key(cls.__name__, key_or_id)
            else:
                key = ndb.Key(urlsafe=key_or_id)
            item = key.get()
            if item:
                if item.accessible(user):
                    return item
        return None

    def accessible(self, user):
        return self.key.parent() == user.key


class UserSearchable(UserAccessible):
    '''
    Parent class for items that can be searched via FTS
    '''

    def get_doc_id(self):
        '''Can override'''
        return str(self.key.id())

    def get_index(self):
        return User.get_search_index(self.key.parent(), self._get_kind())

    def generate_sd(self):
        '''Override'''
        return None

    def doc_from_fields(self, text_fields=None, atom_fields=None, repeated_attom_fields=None):
        fields = []
        if text_fields:
            for tf in text_fields:
                fields.append(search.TextField(name=tf, value=getattr(self, tf)))
        if atom_fields:
            for af in atom_fields:
                fields.append(search.AtomField(name=af, value=getattr(self, af)))
        if repeated_attom_fields:
            for raf in repeated_attom_fields:
                values = getattr(self, raf)
                if values:
                    for val in values:
                        fields.append(search.AtomField(name=raf, value=val))
        sd = search.Document(doc_id=self.get_doc_id(), fields=fields, language='en')
        return sd

    def update_sd(self, delete=False, index_put=True):
        index = self.get_index()
        try:
            doc_id = self.get_doc_id()
            if doc_id:
                if delete:
                    index.delete([doc_id])
                else:
                    sd = self.generate_sd()
                    if sd and index_put:
                        index.put(sd)
            return (sd, index)
        except search.Error, e:
            logging.warning(
                "Search Index Error when updating search doc: %s" % e)
            return (None, None)

    @staticmethod
    def put_sd_batch(items):
        sds = []
        if items:
            index = items[0].get_index()
            for i in items:
                sds.append(i.generate_sd())
            if sds:
                index.put(sds)

    @classmethod
    def Search(cls, user, term, limit=20):
        kind = cls._get_kind()
        index = User.get_search_index(user.key, kind)
        message = None
        success = False
        items = []
        try:
            query_options = search.QueryOptions(limit=limit)
            query = search.Query(query_string=term, options=query_options)
            search_results = index.search(query)
        except Exception, e:
            logging.debug("Error in search api: %s" % e)
            message = str(e)
        else:
            keys = [ndb.Key(kind, sd.doc_id, parent=user.key) for sd in search_results.results if sd]
            items = ndb.get_multi(keys)
            success = True
        return (success, message, items)


class User(ndb.Model):
    """
    Key - ID
    """
    name = ndb.StringProperty()
    email = ndb.StringProperty()
    pw_sha = ndb.StringProperty(indexed=False)
    pw_salt = ndb.StringProperty(indexed=False)
    create_dt = ndb.DateTimeProperty(auto_now_add=True)
    login_dt = ndb.DateTimeProperty(auto_now_add=True)
    level = ndb.IntegerProperty(default=USER.USER)
    timezone = ndb.StringProperty(default="UTC", indexed=False)
    birthday = ndb.DateProperty()
    integrations = ndb.TextProperty()  # Flat JSON dict
    settings = ndb.TextProperty()  # JSON
    sync_services = ndb.StringProperty(repeated=True)  # See AppConstants.INTEGRATIONS
    # Integration IDs
    g_id = ndb.StringProperty()
    fb_id = ndb.StringProperty()
    evernote_id = ndb.StringProperty()

    def __str__(self):
        parts = [x for x in [self.name, self.email] if x]
        return ' - '.join(parts)

    def json(self, is_self=False):
        return {
            'id': self.key.id(),
            'name': self.name,
            'email': self.email,
            'level': self.level,
            'integrations': tools.getJson(self.integrations),
            'settings': tools.getJson(self.settings, {}),
            'timezone': self.timezone,
            'birthday': tools.iso_date(self.birthday) if self.birthday else None,
            'evernote_id': self.evernote_id,
            'sync_services': self.sync_services
        }

    @staticmethod
    def GetByEmail(email, create_if_missing=False, name=None):
        u = User.query().filter(User.email == email.lower()).get()
        if not u and create_if_missing:
            u = User.Create(email=email, name=name)
            u.put()
        return u

    @staticmethod
    def GetByGoogleId(id):
        u = User.query().filter(User.g_id == id).get()
        return u

    @staticmethod
    def SyncActive(sync_integration_id, limit=100):
        multi = type(sync_integration_id) is list
        if multi:
            fltr = User.sync_services.IN(sync_integration_id)
        else:
            fltr = User.sync_services == sync_integration_id
        return User.query().filter(fltr).fetch(limit=limit)

    @staticmethod
    def Create(email=None, g_id=None, name=None, password=None):
        from constants import ADMIN_EMAIL, SENDER_EMAIL, SITENAME, \
            APP_OWNER, DEFAULT_USER_SETTINGS
        if email or g_id:
            u = User(email=email.lower() if email else None, g_id=g_id, name=name)
            if email.lower() == APP_OWNER:
                u.level = USER.ADMIN
            if not password:
                password = tools.GenPasswd()
            u.setPass(password)
            u.Update(settings=DEFAULT_USER_SETTINGS)
            if not tools.on_dev_server():
                mail.send_mail(to=ADMIN_EMAIL, sender=SENDER_EMAIL,
                               subject="[ %s ] New User - %s" % (SITENAME, email),
                               body="That's all")
            return u
        return None

    def Update(self, **params):
        if 'name' in params:
            self.name = params.get('name')
        if 'timezone' in params:
            self.timezone = params.get('timezone')
        if 'birthday' in params:
            self.birthday = tools.fromISODate(params.get('birthday'))
        if 'settings' in params:
            self.settings = json.dumps(params.get('settings'), {})
        if 'fb_id' in params:
            self.fb_id = params.get('fb_id')
        if 'evernote_id' in params:
            self.evernote_id = params.get('evernote_id')
        if 'password' in params:
            self.setPass(pw=params.get('password'))
        if 'sync_services' in params:
            self.sync_services = params.get('sync_services')

    def get(self, cls, id=None):
        if id:
            if isinstance(id, basestring) and id.isdigit():
                id = int(id)
            return cls.get_by_id(id, parent=self.key)

    @classmethod
    def get_search_index(cls, user_key, kind):
        return search.Index(name="FTS_UID:%s_%s" % (user_key.id(), kind))

    def admin(self):
        return self.level == USER.ADMIN

    def setPass(self, pw=None):
        if not pw:
            pw = tools.GenPasswd(length=6)
        self.pw_salt, self.pw_sha = tools.getSHA(pw)
        return pw

    def checkPass(self, pw):
        pw_salt, pw_sha = tools.getSHA(pw, salt=self.pw_salt)
        return self.pw_sha == pw_sha

    def get_timezone(self):
        return self.timezone if self.timezone else "UTC"

    def local_time(self):
        return tools.local_time(self.get_timezone())

    def first_name(self):
        if self.name:
            return self.name.split(' ')[0]
        return ""

    def checkToken(self, token):
        return self.session_id_token == token or self.session_id_token == unicode(quopri.decodestring(token), 'iso_8859-2')

    def get_integration_prop(self, prop, default=None):
        integrations = tools.getJson(self.integrations)
        if integrations:
            return integrations.get(prop, default)
        return default

    def get_setting_prop(self, path, default=None):
        settings = tools.getJson(self.settings)
        if settings:
            cursor = settings
            for i, pi in enumerate(path):
                cursor = cursor.get(pi, {})
                last = i == len(path) - 1
                if last:
                    return cursor if cursor is not None else default
        return default

    def set_integration_prop(self, prop, value):
        integrations = tools.getJson(self.integrations)
        if not integrations:
            integrations = {}
        integrations[prop] = value
        self.integrations = json.dumps(integrations)

    def aes_access_token(self, client_id='google'):
        from common.aes_cypher import AESCipher
        cypher = AESCipher(secrets.AES_CYPHER_KEY)
        msg = cypher.encrypt(json.dumps({
            'client_id': client_id,
            'user_id': self.key.id()
            }))
        return msg

    @staticmethod
    def user_id_from_aes_access_token(access_token):
        from common.aes_cypher import AESCipher
        cypher = AESCipher(secrets.AES_CYPHER_KEY)
        raw = cypher.decrypt(access_token)
        loaded = tools.getJson(raw)
        if loaded:
            return loaded.get('user_id')


class Project(UserAccessible):
    """
    Ongoing projects with links

    Key - ID
    """
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    dt_completed = ndb.DateTimeProperty()
    dt_archived = ndb.DateTimeProperty()
    dt_due = ndb.DateTimeProperty()
    urls = ndb.TextProperty(repeated=True)
    title = ndb.TextProperty()
    subhead = ndb.TextProperty()
    starred = ndb.BooleanProperty(default=False)
    archived = ndb.BooleanProperty(default=False)
    progress = ndb.IntegerProperty(default=0)  # 1 - 10 (-1 disabled)
    progress_ts = ndb.IntegerProperty(repeated=True, indexed=False) # Timestamp (ms) for each progress step (len 10)

    def json(self):
        return {
            'id': self.key.id(),
            'ts_created': tools.unixtime(self.dt_created),
            'ts_completed': tools.unixtime(self.dt_completed),
            'ts_archived': tools.unixtime(self.dt_archived),
            'due': tools.iso_date(self.dt_due),
            'title': self.title,
            'subhead': self.subhead,
            'progress': self.progress,
            'progress_ts': self.progress_ts,
            'archived': self.archived,
            'starred': self.starred,
            'complete': self.is_completed(),
            'urls': [url for url in self.urls if url]
        }

    @staticmethod
    def Active(user):
        return Project.query(ancestor=user.key).filter(Project.archived == False).order(Project.starred).order(-Project.dt_created).fetch(limit=20)

    @staticmethod
    def Fetch(user):
        return Project.query(ancestor=user.key).order(-Project.dt_created).fetch(limit=20)

    @staticmethod
    def Create(user):
        return Project(parent=user.key)

    def Update(self, **params):
        if 'urls' in params:
            self.urls = params.get('urls')
        if 'title' in params:
            self.title = params.get('title')
        if 'subhead' in params:
            self.subhead = params.get('subhead')
        if 'starred' in params:
            self.starred = params.get('starred')
        if 'archived' in params:
            change = self.archived != params.get('archived')
            self.archived = params.get('archived')
            if change and self.archived:
                self.dt_archived = datetime.now()
        if 'progress' in params:
            self.set_progress(params.get('progress'))
        if 'due' in params:
            self.dt_due = params.get('due')

    def set_progress(self, progress):
        regression = progress < self.progress
        if not self.progress_ts:
            self.progress_ts = [0 for x in range(10)]  # Initialize
        self.progress_ts[progress-1] = tools.unixtime()
        logging.debug("set progress -> %s" % progress)
        if regression:
            clear_index = progress
            while clear_index < 10:
                self.progress_ts[clear_index] = 0
                clear_index += 1
        changed = progress != self.progress
        logging.debug(self.progress_ts)
        self.progress = progress
        if changed and self.is_completed():
            self.dt_completed = datetime.now()
        return changed

    def is_completed(self):
        return self.progress == 10


class Task(UserAccessible):
    """
    Tasks (currently not linked with projects)
    For tracking daily 'top tasks'

    Key - ID
    """
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    dt_due = ndb.DateTimeProperty()
    dt_done = ndb.DateTimeProperty()
    title = ndb.TextProperty()
    status = ndb.IntegerProperty(default=TASK.NOT_DONE)
    wip = ndb.BooleanProperty(default=False)
    archived = ndb.BooleanProperty(default=False)

    def json(self):
        return {
            'id': self.key.id(),
            'ts_created': tools.unixtime(self.dt_created),
            'ts_due': tools.unixtime(self.dt_due),
            'ts_done': tools.unixtime(self.dt_done),
            'status': self.status,
            'archived': self.archived,
            'wip': self.wip,
            'title': self.title,
            'done': self.is_done()
        }

    @staticmethod
    def CountCompletedSince(user, since):
        return Task.query(ancestor=user.key).order(-Task.dt_done).filter(Task.dt_done > since).count(limit=None)

    @staticmethod
    def Open(user, limit=10):
        return Task.query(ancestor=user.key).filter(Task.status == TASK.NOT_DONE).order(-Task.dt_created).fetch(limit=limit)

    @staticmethod
    def Recent(user, limit=10):
        return Task.query(ancestor=user.key).filter(Task.archived == False).order(-Task.dt_created).fetch(limit=limit)

    @staticmethod
    def DueInRange(user, start, end, limit=100):
        q = Task.query(ancestor=user.key).order(-Task.dt_due)
        if start:
            q = q.filter(Task.dt_due >= start)
        if end:
            q = q.filter(Task.dt_due <= end)
        return q.fetch(limit=limit)

    @staticmethod
    def Create(user, title, due=None):
        if not due:
            tz = user.get_timezone()
            local_now = tools.local_time(tz)
            task_prefs = user.get_setting_prop(['tasks', 'preferences'], {})
            same_day_hour = int(task_prefs.get('same_day_hour', 16))
            due_hour = int(task_prefs.get('due_hour', 22))
            schedule_for_same_day = local_now.hour < same_day_hour
            time_due = time(due_hour, 0)
            due = datetime.combine(local_now.date(), time_due)
            if not schedule_for_same_day:
                due += timedelta(days=1)
            if due:
                due = tools.server_time(tz, due)
        return Task(title=tools.capitalize(title), dt_due=due, parent=user.key)

    def Update(self, **params):
        from constants import TASK_DONE_REPLIES
        message = None
        if 'title' in params:
            self.title = params.get('title')
        if 'status' in params:
            change = self.status != params.get('status')
            self.status = params.get('status')
            if change and self.is_done():
                self.dt_done = datetime.now()
                self.wip = False
                message = random.choice(TASK_DONE_REPLIES)
        if 'archived' in params:
            self.archived = params.get('archived')
            if self.archived:
                self.wip = False
        if 'wip' in params:
            self.wip = params.get('wip')
        return message

    def mark_done(self):
        message = self.Update(status=TASK.DONE)
        return message

    def is_done(self):
        return self.status == TASK.DONE

    def archive(self):
        self.archived = True
        self.wip = False


class Habit(UserAccessible):
    """
    Key - ID
    """
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    name = ndb.TextProperty()
    description = ndb.TextProperty()
    color = ndb.TextProperty()
    tgt_weekly = ndb.IntegerProperty(indexed=False)
    archived = ndb.BooleanProperty(default=False)
    icon = ndb.TextProperty()

    def json(self):
        return {
            'id': self.key.id(),
            'ts_created': tools.unixtime(self.dt_created),
            'name': self.name,
            'description': self.description,
            'color': self.color,
            'archived': self.archived,
            'tgt_weekly': self.tgt_weekly,
            'icon': self.icon
        }

    def slug_name(self):
        return tools.strip_symbols(self.name.replace(' ','')).lower().strip()

    @staticmethod
    def All(user):
        return Habit.query(ancestor=user.key).fetch(limit=20)

    @staticmethod
    def Active(user):
        return Habit.query(ancestor=user.key).filter(Habit.archived == False).fetch(limit=8)

    @staticmethod
    def Create(user):
        return Habit(parent=user.key)

    def Update(self, **params):
        if 'name' in params:
            self.name = params.get('name').title()
        if 'description' in params:
            self.description = params.get('description').title()
        if 'color' in params:
            self.color = params.get('color')
        if 'icon' in params:
            self.icon = params.get('icon')
        if 'archived' in params:
            self.archived = params.get('archived')
        if 'tgt_weekly' in params:
            self.tgt_weekly = params.get('tgt_weekly')


class HabitDay(UserAccessible):
    """
    Key - ID: habit:[habit_id]_day:[iso_date]
    """
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    dt_updated = ndb.DateTimeProperty(auto_now_add=True)
    habit = ndb.KeyProperty(Habit)
    date = ndb.DateProperty()
    done = ndb.BooleanProperty(default=False)
    committed = ndb.BooleanProperty(default=False)

    def json(self):
        return {
            'id': self.key.id(),
            'ts_created': tools.unixtime(self.dt_created),
            'ts_updated': tools.unixtime(self.dt_updated),
            'habit_id': self.habit.id(),
            'done': self.done,
            'committed': self.committed
        }

    @staticmethod
    def Range(user, habits, since_date, until_date=None):
        '''
        Fetch habit days for specified habits in date range

        Args:
            habits (list of Habit() objects)
            ...

        Returns:
            list: HabitDay() ordered sequentially

        '''
        today = datetime.today()
        if not until_date:
            until_date = today
        cursor = since_date
        ids = []
        while cursor <= until_date:
            for h in habits:
                ids.append(ndb.Key('HabitDay', HabitDay.ID(h, cursor), parent=user.key))
            cursor += timedelta(days=1)
        if ids:
            return [hd for hd in ndb.get_multi(ids) if hd]
        return []

    @staticmethod
    def ID(habit, date):
        return "habit:%s_day:%s" % (habit.key.id(), tools.iso_date(date))

    @staticmethod
    def Create(user, habit, date):
        id = HabitDay.ID(habit, date)
        return HabitDay(id=id, habit=habit.key, date=date, parent=user.key)

    def Update(self, **params):
        if 'done' in params:
            self.done = params.get('done')
        self.dt_updated = datetime.now()

    @staticmethod
    def Toggle(habit, date, force_done=False):
        id = HabitDay.ID(habit, date)
        hd = HabitDay.get_or_insert(id,
                                    habit=habit.key,
                                    date=date,
                                    parent=habit.key.parent())
        if not force_done or not hd.done:
            # If force_done, only toggle if not done
            hd.toggle()
        hd.put()
        return (hd.done, hd)

    @staticmethod
    def Commit(habit, date=None):
        if not date:
            date = datetime.today()
        id = HabitDay.ID(habit, date)
        hd = HabitDay.get_or_insert(id,
                                    habit=habit.key,
                                    date=date,
                                    parent=habit.key.parent())
        hd.commit()
        hd.put()
        return hd

    def toggle(self):
        self.dt_updated = datetime.now()
        self.done = not self.done
        return self.done

    def commit(self):
        if not self.done:
            self.committed = True


class JournalTag(UserAccessible):
    """
    Stores frequent activities/tags/people for daily journal

    Key - ID: Full tag with @/#, e.g. '#DinnerOut', '@BarackObama'
    """
    dt_added = ndb.DateTimeProperty(auto_now_add=True)
    name = ndb.TextProperty()
    type = ndb.IntegerProperty(default=JOURNALTAG.PERSON)

    def json(self):
        return {
            'id': self.key.id(),
            'name': self.name,
            'type': self.type
        }

    @staticmethod
    def All(user, limit=400):
        return JournalTag.query(ancestor=user.key).fetch(limit=limit)

    @staticmethod
    def Key(user, name, prefix='@'):
        if name:
            name = tools.capitalize(name)
            return ndb.Key('JournalTag', prefix+name, parent=user.key)

    @staticmethod
    def CreateFromText(user, text):
        people = re.findall(r'@([a-zA-Z]{3,30})', text)
        hashtags = re.findall(r'#([a-zA-Z]{3,30})', text)
        new_jts = []
        all_jts = []
        people_ids = [JournalTag.Key(user, p) for p in people]
        hashtag_ids = [JournalTag.Key(user, ht, prefix='#') for ht in hashtags]
        existing_tags = ndb.get_multi(people_ids + hashtag_ids)
        for existing_tag, key in zip(existing_tags, people_ids + hashtag_ids):
            if not existing_tag:
                prefix = key.id()[0]
                type = JOURNALTAG.HASHTAG if prefix == '#' else JOURNALTAG.PERSON
                jt = JournalTag(id=key.id(), name=key.id()[1:], type=type, parent=user.key)
                new_jts.append(jt)
                all_jts.append(jt)
            else:
                all_jts.append(existing_tag)
        ndb.put_multi(new_jts)
        return all_jts

    def person(self):
        return self.type == JOURNALTAG.PERSON


class MiniJournal(UserAccessible):
    """
    Key - ID: [ISO_date]
    Capture some basic data points from the day via 1-2 questions?
    Questions defined on client side.

    Optionally collect and track completion of top 3 tasks (decided tonight for tomorrow)
    """
    date = ndb.DateProperty()  # Date for entry
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    data = ndb.TextProperty()  # JSON (keys are data names, values are responses)
    tags = ndb.KeyProperty(repeated=True)  # IDs of JournalTags()
    location = ndb.GeoPtProperty()

    def json(self):
        res = {
            'id': self.key.id(),
            'iso_date': tools.iso_date(self.date),
            'data': tools.getJson(self.data),
            'tags': [tag.id() for tag in self.tags]
        }
        if self.location:
            res.update({
                'lat': self.location.lat,
                'lon': self.location.lon
            })
        return res

    @staticmethod
    def Create(user, date=None):
        if not date:
            date = MiniJournal.CurrentSubmissionDate()
        id = tools.iso_date(date)
        return MiniJournal(id=id, date=date, parent=user.key)

    @staticmethod
    def Fetch(user, start, end):
        journal_keys = []
        iso_dates = []
        if start < end:
            date_cursor = start
            while date_cursor < end:
                date_cursor += timedelta(days=1)
                iso_date = tools.iso_date(date_cursor)
                journal_keys.append(ndb.Key('MiniJournal', iso_date, parent=user.key))
                iso_dates.append(iso_date)
        return ([j for j in ndb.get_multi(journal_keys) if j], iso_dates)

    @staticmethod
    def Get(user, date=None):
        if not date:
            date = MiniJournal.CurrentSubmissionDate()
        id = tools.iso_date(date)
        return MiniJournal.get_by_id(id, parent=user.key)

    @staticmethod
    def CurrentSubmissionDate():
        HOURS_BACK = 8
        now = datetime.now()
        return (now - timedelta(hours=HOURS_BACK)).date()

    def Update(self, **params):
        if 'data' in params:
            self.data = json.dumps(params.get('data'))
        if 'lat' in params and 'lon' in params:
            gp = ndb.GeoPt("%s, %s" % (params.get('lat'), params.get('lon')))
            self.location = gp
        if 'tags' in params:
            self.tags = params.get('tags', [])

    def parse_tags(self):
        user = self.key.parent().get()
        questions = tools.getJson(user.settings, {}).get('journals', {}).get('questions', [])
        parse_questions = [q.get('name') for q in questions if q.get('parse_tags')]
        tags = []
        for q in parse_questions:
            response_text = tools.getJson(self.data).get(q)
            if response_text:
                tags.extend(JournalTag.CreateFromText(user, response_text))
        for tag in tags:
            if tag.key not in self.tags:
                self.tags.append(tag.key)

    def get_data_value(self, prop):
        data = tools.getJson(self.data, {})
        return data.get(prop)


class Snapshot(UserAccessible):
    """
    Key - ID
    Randomly collect data points throughout day/week (frequency customizable)
    Metrics:
        - Activity
        - Location (generic, e.g. home/office/restaurant)
        - People (who with)
    Passive metrics:
        - GPS location, if available

    """
    ACTIVITY_SEPS = [" - ", ": "]

    date = ndb.DateProperty(auto_now_add=True)  # Date for entry
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    activity = ndb.StringProperty()
    activity_sub = ndb.StringProperty()
    place = ndb.StringProperty()
    people = ndb.StringProperty(repeated=True)
    metrics = ndb.TextProperty()  # JSON
    location = ndb.GeoPtProperty()

    def json(self):
        res = {
            'id': self.key.id(),
            'ts': tools.unixtime(self.dt_created),
            'iso_date': tools.iso_date(self.date),
            'metrics': tools.getJson(self.metrics),
            'people': self.people,
            'place': self.place,
            'activity': self.activity,
            'activity_sub': self.activity_sub
        }
        if self.location:
            res.update({
                'lat': self.location.lat,
                'lon': self.location.lon
            })
        return res

    @staticmethod
    def Create(user, activity=None, place=None, people=None, metrics=None, lat=None, lon=None, date=None):
        if not date:
            date = datetime.now()
        location = activity_sub = None
        if lat and lon:
            gp = ndb.GeoPt("%s, %s" % (lat, lon))
            location = gp
        if activity:
            for sep in Snapshot.ACTIVITY_SEPS:
                if sep in activity:
                    act_list = activity.split(sep)
                    if act_list:
                        activity = act_list[0]
                    if len(act_list) > 1:
                        activity_sub = act_list[1]
                    break
        return Snapshot(dt_created=date, place=place, people=people if people else [],
                        activity=activity, activity_sub=activity_sub, metrics=json.dumps(metrics) if metrics else None,
                        location=location, parent=user.key)

    @staticmethod
    def Recent(user, limit=500):
        return Snapshot.query().order(-Snapshot.dt_created).fetch(limit=limit)

    def get_data_value(self, prop):
        metrics = tools.getJson(self.metrics, {})
        return metrics.get(prop)


class Event(UserAccessible):
    """
    Key - ID

    Events (single date or ranges) that are meaningful
    """
    date_start = ndb.DateProperty()
    date_end = ndb.DateProperty()
    title = ndb.TextProperty()
    details = ndb.TextProperty()
    color = ndb.StringProperty()  # Hex
    private = ndb.BooleanProperty(default=True)
    type = ndb.IntegerProperty(default=EVENT.PERSONAL)

    def json(self):
        res = {
            'id': self.key.id(),
            'date_start': tools.iso_date(self.date_start),
            'date_end': tools.iso_date(self.date_end),
            'title': self.title,
            'details': self.details,
            'color': self.color,
            'private': self.private,
            'single': self.single(),
            'type': self.type
        }
        return res

    @staticmethod
    def Fetch(user, limit=20, offset=0):
        return Event.query(ancestor=user.key).order(Event.date_start).fetch(limit=limit, offset=offset)

    @staticmethod
    def Create(user, date_start, date_end=None, title=None, details=None, color=None, **params):
        if not date_end:
            date_end = date_start
        return Event(date_start=date_start, date_end=date_end, title=title, details=details,
                     color=color, parent=user.key)

    def Update(self, **params):
        if 'title' in params:
            self.title = params.get('title')
        if 'details' in params:
            self.details = params.get('details')
        if 'color' in params:
            self.color = params.get('color')
        if 'date_start' in params:
            self.date_start = params.get('date_start')
        if 'date_end' in params:
            self.date_end = params.get('date_end')
        if 'type' in params:
            self.type = params.get('type')

    def single(self):
        return self.date_start == self.date_end


class Goal(UserAccessible):
    """
    Key - ID: [YYYY] if annual goal, [YYYY-MM] if monthly goal

    Annual/monthly goals (currently captured in timeline gsheet)

    """
    date = ndb.DateProperty()  # Date for (first day of month or year)
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    text = ndb.TextProperty(repeated=True)  # Can have multiple goals for period
    assessment = ndb.IntegerProperty()  # How'd we do (1-5)

    def json(self):
        res = {
            'id': self.key.id(),
            'iso_date': tools.iso_date(self.date),
            'text': self.text,
            'assessment': self.assessment,
            'annual': self.annual(),
            'monthly': self.monthly(),
            'longterm': self.longterm()
        }
        if self.date:
            res['month'] = self.date.month
        return res

    @staticmethod
    def Recent(user):
        goals = Goal.query(ancestor=user.key).order(-Goal.dt_created).fetch(limit=13)
        return goals

    @staticmethod
    def Year(user, year):
        jan_1 = datetime(year, 1, 1).date()
        goals = Goal.query(ancestor=user.key).filter(Goal.date >= jan_1).fetch(limit=13)
        return sorted(filter(lambda g: g.date.year == year and not g.annual(), goals),
                      key=lambda g: g.date)

    @staticmethod
    def Current(user, which="all"):
        date = tools.local_time(user.get_timezone(), datetime.today())
        keys = []
        if which in ["all", "year"]:
            annual_id = ndb.Key('Goal', datetime.strftime(date, "%Y"), parent=user.key)
            keys.append(annual_id)
        if which in ["all", "month"]:
            monthly_id = ndb.Key('Goal', datetime.strftime(date, "%Y-%m"), parent=user.key)
            keys.append(monthly_id)
        if which in ["all", "longterm"]:
            monthly_id = ndb.Key('Goal', datetime.strftime(date, "longterm"), parent=user.key)
            keys.append(monthly_id)
        goals = ndb.get_multi(keys)
        return [g for g in goals]

    @staticmethod
    def Create(user, id, date=None):
        g = Goal(id=id, parent=user.key)
        if g.monthly(id=id) and not date:
            g.date = tools.fromISODate(id + "-01")
        elif g.annual(id=id) and not g.date:
            first_of_year = datetime(int(id), 1, 1)
            date = first_of_year
        return g

    @staticmethod
    def CreateMonthly(user, date=None):
        if not date:
            date = datetime.now()
        id = datetime.strftime(date, "%Y-%m")
        return Goal.Create(user, id)

    def Update(self, **params):
        if 'text' in params:
            self.text = params.get('text')
        if 'assessment' in params:
            a = params.get('assessment')
            if a:
                self.assessment = int(a)

    def type(self):
        return 'annual' if self.annual() else 'monthly'

    def annual(self, id=None):
        if not id:
            id = self.key.id()
        return len(id) == 4

    def monthly(self, id=None):
        if not id:
            id = self.key.id()
        return len(id) == 7

    def longterm(self):
        return str(self.key.id()) == 'longterm'


class TrackingDay(UserAccessible):
    """
    Key - ID: [YYYY-MM-DD]

    Abstract model to store data for a particular day

    """
    date = ndb.DateProperty()
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    data = ndb.TextProperty()  # Flat JSON object

    def json(self):
        return {
            'id': self.key.id(),
            'iso_date': tools.iso_date(self.date),
            'data': tools.getJson(self.data)
        }

    @staticmethod
    def Create(user, date):
        id = tools.iso_date(date)
        return TrackingDay.get_or_insert(id, date=date, parent=user.key)

    @staticmethod
    def Range(user, dt_start, dt_end):
        return TrackingDay.query(ancestor=user.key).order(-TrackingDay.date) \
            .filter(TrackingDay.date >= dt_start) \
            .filter(TrackingDay.date <= dt_end) \
            .fetch()

    def Update(self, **params):
        if 'data' in params:
            self.data = json.dumps(params.get('data'))

    def set_properties(self, property_dict):
        data = tools.getJson(self.data, default={})
        data.update(property_dict)
        self.data = json.dumps(data)


class Readable(UserSearchable):
    """
    Readable things (books / articles)

    Key - ID [source]:[source id]

    """
    source_id = ndb.TextProperty()
    dt_added = ndb.DateTimeProperty()
    dt_read = ndb.DateTimeProperty()
    title = ndb.TextProperty()  # Can have multiple goals for period
    author = ndb.TextProperty()
    slug = ndb.StringProperty()  # Uppercase TITLE (AUTHOR LAST NAME), symbols removed
    image_url = ndb.TextProperty()
    url = ndb.TextProperty()  # Original source URL
    favorite = ndb.BooleanProperty(default=False)
    type = ndb.IntegerProperty(default=READABLE.ARTICLE)
    excerpt = ndb.TextProperty()
    notes = ndb.TextProperty()
    has_notes = ndb.BooleanProperty(default=False)
    source = ndb.TextProperty()  # e.g. 'pocket', 'goodreads'
    tags = ndb.TextProperty(repeated=True)  # Lowercase
    read = ndb.BooleanProperty(default=False)
    word_count = ndb.IntegerProperty()

    def __str__(self):
        return "%s (%s)" % (self.title, self.author)

    def json(self):
        return {
            'id': self.key.id(),
            'title': self.title,
            'author': self.author,
            'slug': self.slug,
            'favorite': self.favorite,
            'image_url': self.image_url,
            'url': self.url,  # Original url
            'source_url': self.get_source_url(),
            'type': self.type,
            'source': self.source,
            'notes': self.notes,
            'has_notes': self.has_notes,
            'read': self.read,
            'tags': self.tags,
            'word_count': self.word_count,
            'date_read': tools.iso_date(self.dt_read) if self.dt_read else None
        }

    @staticmethod
    def Fetch(user, favorites=False, with_notes=False, unread=False, read=False,
              limit=30, since=None, until=None, offset=0, keys_only=False):
        q = Readable.query(ancestor=user.key)
        ordering_prop = Readable.dt_added if not read else Readable.dt_read
        if with_notes:
            q = q.filter(Readable.has_notes == True)
        elif favorites:
            q = q.filter(Readable.favorite == True)
        elif unread:
            q = q.filter(Readable.read == False)
        elif read:
            q = q.filter(Readable.read == True)
        q = q.order(-ordering_prop)
        if since:
            q = q.filter(ordering_prop >= tools.fromISODate(since))
        if until:
            q = q.filter(ordering_prop <= tools.fromISODate(until))
        return q.fetch(limit=limit, offset=offset, keys_only=keys_only)

    @staticmethod
    def CreateOrUpdate(user, source_id, title=None, url=None,
                       type=READABLE.ARTICLE, source=None,
                       author=None, image_url=None, excerpt=None,
                       tags=None, read=False, favorite=False,
                       dt_read=None, notes=None,
                       word_count=0, dt_added=None, **params):
        if title and source:
            if source_id is None:
                m = hashlib.md5()
                m.update(tools.removeNonAscii(title))
                source_id = m.hexdigest()
            if tags is None:
                tags = []
            tags = [t.lower() for t in tags if t]
            if not dt_added:
                dt_added = datetime.now()
            id = source + ':' + source_id
            r = Readable.get_or_insert(id, parent=user.key, source_id=source_id,
                                       title=title, url=url,
                                       type=type, source=source, read=read,
                                       dt_added=dt_added, notes=notes,
                                       excerpt=excerpt, favorite=favorite,
                                       tags=tags, dt_read=dt_read,
                                       image_url=image_url, author=author,
                                       word_count=word_count)
            if not r.slug:
                r.generate_slug()
            r.has_notes = bool(r.notes)
            return r

    @staticmethod
    def GetByTitleAuthor(user, author, title):
        slug = Readable.Slug(author, title)
        return Readable.GetBySlug(user, slug)

    @staticmethod
    @auto_cache()
    def GetBySlug(user, slug):
        return Readable.query(ancestor=user.key).filter(Readable.slug == slug).get()

    def Update(self, **params):
        if 'read' in params:
            change = self.read != params.get('read')
            self.read = params.get('read')
            if change and self.read:
                self.dt_read = datetime.now()
        if 'favorite' in params:
            self.favorite = params.get('favorite')
        if 'dt_read' in params:
            self.dt_read = params.get('dt_read')
        if 'source' in params:
            self.source = params.get('source')
        if 'excerpt' in params:
            self.excerpt = params.get('excerpt')
        if 'notes' in params:
            self.notes = params.get('notes')
            self.has_notes = bool(self.notes)
        if 'title' in params:
            self.title = params.get('title')
        if 'url' in params:
            self.url = params.get('url')
        if 'type' in params:
            self.type = params.get('type')
        if 'source_id' in params:
            self.source_id = params.get('source_id')
        if 'image_url' in params:
            self.image_url = params.get('image_url')
        if 'tags' in params:
            self.tags = params.get('tags')
        if 'author' in params:
            self.author = params.get('author')
        if 'word_count' in params:
            self.word_count = params.get('word_count')
        if not self.slug:
            self.generate_slug()
        self.update_sd()  # doc put

    @staticmethod
    def Slug(author, title):
        if title:
            slug = tools.strip_symbols(title).upper()
            if author:
                slug += " (%s)" % tools.parse_last_name(tools.strip_symbols(author)).upper()
            return slug

    def generate_slug(self):
        if self.title:
            self.slug = Readable.Slug(self.author, self.title)
            return self.slug

    def generate_sd(self):
        return self.doc_from_fields(text_fields=['title', 'notes', 'author'],
                                    repeated_attom_fields=['tags'],
                                    atom_fields=['url'])

    def print_type(self):
        return READABLE.LABELS.get(self.type)

    def get_source_url(self):
        if self.source == 'pocket':
            return "https://getpocket.com/a/read/%s" % self.source_id


class Quote(UserSearchable):
    """
    Quotes

    Key - ID md5([source + content])

    """
    source_id = ndb.TextProperty()
    dt_added = ndb.DateTimeProperty(auto_now_add=True)
    readable = ndb.KeyProperty()
    source = ndb.TextProperty()  # Title of piece, person
    link = ndb.TextProperty()
    location = ndb.TextProperty()  # (optional) location in piece
    tags = ndb.StringProperty(repeated=True)  # lower case, symbols removed
    content = ndb.TextProperty()

    def json(self):
        return {
            'id': self.key.id(),
            'source': self.source,
            'link': self.link,
            'content': self.content,
            'location': self.location,
            'readable': self.readable.id() if self.readable else None,
            'iso_date': tools.iso_date(self.dt_added) if self.dt_added else None,
            'tags': self.tags
        }

    @staticmethod
    def Create(user, source, content, dt_added=None, location=None, **params):
        if source and content:
            m = hashlib.md5()
            m.update('|'.join([tools.removeNonAscii(x) for x in [source, content]]))
            id = m.hexdigest()
            if not dt_added:
                dt_added = datetime.now()
            q = Quote(id=id, source=source, content=content,
                         location=location,
                         dt_added=dt_added, parent=user.key)
            q.lookup_readable(user)
            return q

    @staticmethod
    def Fetch(user, readable_id=None, limit=50, offset=0, keys_only=False):
        query = Quote.query(ancestor=user.key).order(-Quote.dt_added)
        if readable_id:
            key = ndb.Key('User', user.key.id(), 'Readable', readable_id)
            query = query.filter(Quote.readable == key)
        return query.fetch(limit=limit, offset=offset, keys_only=keys_only)

    def Update(self, **params):
        if 'source' in params:
            self.source = params.get('source')
        if 'content' in params:
            self.content = params.get('content')
        if 'location' in params:
            self.location = params.get('location')
        if 'link' in params:
            self.link = params.get('link')
        if 'tags' in params:
            logging.debug(params)
            tags = params.get('tags', [])
            if tags:
                self.tags = tags
        self.update_sd()  # doc put

    def generate_sd(self):
        return self.doc_from_fields(text_fields=['source', 'content'],
                                    repeated_attom_fields=['tags'],
                                    atom_fields=['link'])

    def source_slug(self):
        pattern = r"(?P<title>.*) \((?P<author>.*)\)"
        match = re.search(pattern, self.source, flags=re.M)
        author = None
        if match:
            mdict = match.groupdict()
            title = mdict.get('title')
            author = tools.parse_last_name(mdict.get('author'))
        else:
            title = self.source
        if title:
            return Readable.Slug(author, title)

    def lookup_readable(self, user):
        USE_FTS = True
        if self.source:
            if USE_FTS:
                # Lookup via readable full-text-search
                lookup_title_quoted = "\"%s\"" % re.sub(r'\((.*)\)$', '', self.source.replace("\"", "\\\"")).strip()
                success, message, readables = Readable.Search(user, lookup_title_quoted)
                if success:
                    if len(readables) == 1:
                        # Non-ambiguous result, link it
                        r = readables[0]
                        self.readable = r.key
                        return r
            else:
                # Lookup via slug query
                slug = self.source_slug()
                if slug:
                    r = Readable.GetBySlug(user, slug)
                    if r:
                        self.readable = r.key
                        return r


class Report(UserAccessible):
    """
    Key - ID
    """
    dt_created = ndb.DateTimeProperty(auto_now_add=True)
    dt_generated = ndb.DateTimeProperty()
    gcs_files = ndb.StringProperty(repeated=True, indexed=False)
    title = ndb.StringProperty()
    storage_type = ndb.IntegerProperty(default=REPORT.GCS_CLIENT, indexed=False)
    status = ndb.IntegerProperty(default=REPORT.CREATED)
    type = ndb.IntegerProperty(default=REPORT.HABIT_REPORT)
    ftype = ndb.IntegerProperty(default=REPORT.CSV, indexed=False)
    extension = ndb.StringProperty(default="csv", indexed=False)
    specs = ndb.TextProperty()  # JSON, e.g. date filters etc

    def __str__(self):
        return "%s (%s)" % (self.title, self.print_type())

    def json(self):
        return {
            'key': self.key.urlsafe(),
            'id': self.key.id(),
            'title': self.title,
            'status': self.status,
            'serve_url': self.serving_url(),
            'type': self.type,
            'ftype': self.ftype,
            'extension': self.extension,
            'ts_created': tools.unixtime(self.dt_created),
            'ts_generated': tools.unixtime(self.dt_generated),
            'filenames': self.gcs_files
        }

    @staticmethod
    def Fetch(user, limit=50):
        return Report.query(ancestor=user.key).order(-Report.dt_created).fetch(limit=limit)

    @staticmethod
    def Create(user, title="Unnamed Report", type=REPORT.HABIT_REPORT, specs=None, ftype=None):
        logging.debug("Requesting report creation, type %d specs: %s" % (type, specs))
        r = Report(title=title, type=type, parent=user.key)
        if specs:
            r.set_specs(specs)
        r.storage_type = REPORT.GCS_CLIENT
        if ftype is not None:
            r.ftype = ftype
        else:
            r.ftype = REPORT.CSV
        r.get_extension()
        return r

    def get_duration(self):
        if self.dt_created and self.dt_generated:
            return tools.total_seconds(self.dt_generated - self.dt_created)
        return 0

    def get_specs(self):
        if self.specs:
            return json.loads(self.specs)
        return {}

    def is_done(self):
        return self.status == REPORT.DONE

    def is_generating(self):
        return self.status == REPORT.GENERATING

    def set_specs(self, data):
        self.specs = json.dumps(data)

    def generate_title(self, _title, ts_start=None, ts_end=None, **kwargs):
        title = _title
        start_text = end_text = None
        if ts_start:
            start_text = tools.sdatetime(tools.dt_from_ts(ts_start))
        if ts_end:
            end_text = tools.sdatetime(tools.dt_from_ts(ts_end))
        if start_text and end_text:
            title += " (%s - %s)" % (start_text, end_text)
        elif start_text:
            title += " Since %s" % start_text
        elif end_text:
            title += " Until %s" % end_text
        for key, val in kwargs.items():
            if key and val is not None:
                title += " %s:%s" % (key, val)
        self.title = title

    def filename(self, ext=None, piece=None):
        _piece = ""
        if piece is not None:
            _piece = self.gcs_filenames[piece-1]
        _ext = ext if ext else self.extension
        fn = "%s%s.%s" % (self.title, _piece, _ext)
        return fn

    def get_extension(self):
        self.extension = REPORT.EXTENSIONS.get(self.ftype)

    def print_type(self): return REPORT.TYPE_LABELS.get(self.type)

    def print_status(self): return REPORT.STATUS_LABELS.get(self.status)

    @staticmethod
    def content_type(extension):
        if extension in ['xls', 'xlsx']:
            return "application/ms-excel"
        elif extension == 'csv':
            return "text/csv"
        else:
            return None

    def run(self, start_cursor=None):
        """Begins report generation"""
        from reports import HabitReportWorker, TaskReportWorker, GoalReportWorker, JournalReportWorker
        worker_lookup = {
            REPORT.HABIT_REPORT: HabitReportWorker,
            REPORT.TASK_REPORT: TaskReportWorker,
            REPORT.GOAL_REPORT: GoalReportWorker,
            REPORT.JOURNAL_REPORT: JournalReportWorker
        }
        worker_class = worker_lookup.get(self.type)
        worker = None
        if worker_class:
            worker = worker_class(self.key)
            if worker and self.status not in [REPORT.ERROR, REPORT.CANCELLED]:
                worker.run(start_cursor=start_cursor)
            else:
                logging.error("Worker not created or invalid status for run(): type %d" % self.type)

    def finish(self):
        '''Finalize report'''
        self.status = REPORT.DONE
        self.dt_generated = datetime.now()

    def serving_url(self):
        url = None
        if self.storage_type == REPORT.GCS_CLIENT:
            url = "/api/report/serve?rkey=%s" % self.key.urlsafe()
        return url

    def get_gcs_file(self, index=0):
        # we actually don't anticipate more than 1 gcsfiles anymore
        if self.gcs_files and len(self.gcs_files) >= index + 1:
            return self.gcs_files[index]
        return None

    def delete_gcs_files(self):
        import cloudstorage as gcs
        if self.gcs_files:
            for f in self.gcs_files:
                try:
                    gcs.delete(f)
                    self.gcs_files.remove(f)
                except gcs.NotFoundError, e:
                    logging.debug("File %s not found on gcs" % f)

    def clean_delete(self, self_delete=True):
        self.delete_gcs_files()
        if self_delete:
            self.key.delete()
