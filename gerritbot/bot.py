#! /usr/bin/env python

#    Copyright 2011 OpenStack LLC
#    Copyright 2012 Hewlett-Packard Development Company, L.P.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# The configuration file should look like:
"""
[ircbot]
nick=NICKNAME
pass=PASSWORD
server=irc.freenode.net
port=6667
force_ssl=false
server_password=SERVERPASS
channel_config=/path/to/yaml/config
pid=/path/to/pid_file
use_mqtt=True

[gerrit]
user=gerrit2
key=/path/to/id_rsa
host=review.example.com
port=29418

[mqtt]
host=example.com
port=1883
websocket=False
"""

# The yaml channel config should look like:
"""
openstack-dev:
    events:
      - patchset-created
      - change-merged
    projects:
      - openstack/nova
      - openstack/swift
    branches:
      - master
"""

import ConfigParser
import daemon
import irc.bot
import json
import logging.config
import os
import re
import ssl
import sys
import threading
import time
import yaml

import paho.mqtt.client as mqtt

try:
    import daemon.pidlockfile
    pid_file_module = daemon.pidlockfile
except Exception:
    # as of python-daemon 1.6 it doesn't bundle pidlockfile anymore
    # instead it depends on lockfile-0.9.1
    import daemon.pidfile
    pid_file_module = daemon.pidfile


# https://bitbucket.org/jaraco/irc/issue/34/
# irc-client-should-not-crash-on-failed
# ^ This is why pep8 is a bad idea.
irc.client.ServerConnection.buffer_class.errors = 'replace'


class GerritBot(irc.bot.SingleServerIRCBot):
    def __init__(self, channels, nickname, password, server, port=6667,
                 force_ssl=False, server_password=None):
        if force_ssl or port == 6697:
            factory = irc.connection.Factory(wrapper=ssl.wrap_socket)
            super(GerritBot, self).__init__([(server, port, server_password)],
                                            nickname, nickname,
                                            connect_factory=factory)
        else:
            super(GerritBot, self).__init__([(server, port, server_password)],
                                            nickname, nickname)
        self.channel_list = channels
        self.nickname = nickname
        self.password = password
        self.log = logging.getLogger('gerritbot')

    def on_nicknameinuse(self, c, e):
        c.nick(c.get_nickname() + "_")
        c.privmsg("nickserv", "identify %s " % self.password)
        c.privmsg("nickserv", "ghost %s %s" % (self.nickname, self.password))
        c.privmsg("nickserv", "release %s %s" % (self.nickname, self.password))
        time.sleep(1)
        c.nick(self.nickname)
        self.log.info('Nick previously in use, recovered.')

    def on_welcome(self, c, e):
        self.log.info('Identifying with IRC server.')
        c.privmsg("nickserv", "identify %s " % self.password)
        for channel in self.channel_list:
            c.join(channel)
            self.log.info('Joined channel %s' % channel)
            time.sleep(0.1)

    def send(self, channel, msg):
        self.log.info('Sending "%s" to %s' % (msg, channel))
        try:
            self.connection.privmsg(channel, msg)
            time.sleep(0.1)
        except Exception:
            self.log.exception('Exception sending message:')
            self.connection.reconnect()


class Gerrit(threading.Thread):
    TARGET_USER_ID = 'username'
    SOURCE_USER_ID = 'name'
    WILDCARD = '*'
    EMPTY_GIT_HASH = '0000000000000000000000000000000000000000'

    def __init__(self, ircbot, channel_config, server,
                 username, port=29418, keyfile=None):
        super(Gerrit, self).__init__()
        self.ircbot = ircbot
        self.channel_config = channel_config
        self.log = logging.getLogger('gerritbot')
        self.server = server
        self.username = username
        self.port = port
        self.keyfile = keyfile
        self.connected = False
        self.reviewer_list = {}

    def connect(self):
        # Import here because it needs to happen after daemonization
        import gerritlib.gerrit
        try:
            self.gerrit = gerritlib.gerrit.Gerrit(
                self.server, self.username, self.port, self.keyfile)
            self.gerrit.startWatching()
            self.log.info('Start watching Gerrit event stream.')
            self.connected = True
        except Exception:
            self.log.exception('Exception while connecting to gerrit')
            self.connected = False
            # Delay before attempting again.
            time.sleep(1)

    def add_reviewer(self, data):
        key = data['change']['id']
        value = data['reviewer'][Gerrit.TARGET_USER_ID]
        if not self.reviewer_list.has_key(key):
            self.reviewer_list[key] = []
        self.reviewer_list[key].append(value)
        self.reviewer_list[key].sort()

    def clear_reviewers(self, data):
        key = data['change']['id']
        if self.reviewer_list.has_key(key):
            del self.reviewer_list[key]

    def get_reviewers(self, data):
        key = data['change']['id']
        if self.reviewer_list.has_key(key):
            return self.reviewer_list[key]
        return []

    def channel_has_event(self, event):
        return (self.channel_config.events.get(Gerrit.WILDCARD, False) or
                event in self.channel_config.events.get(event, set()))

    def channel_has_project(self, project):
        return (self.channel_config.projects.get(Gerrit.WILDCARD, False) or
                project in self.channel_config.projects.get(project, set()))

    def channel_has_branch(self, branch):
        return (self.channel_config.branches.get(Gerrit.WILDCARD, False) or
                branch in self.channel_config.branches.get(branch, set()))

    def project_description(self, data):
        return '%s: %s  %s' % (data['change']['project'],
                               data['change']['subject'],
                               data['change']['url'])

    def patchset_size_str(self, data):
        size_ins = data['patchSet']['sizeInsertions']
        size_del = abs(data['patchSet']['sizeDeletions'])
        return '+%d/-%d' % (size_ins, size_del)

    def patchset_created(self, channel, data):
        if data['patchSet']['number'] == '1':
            reviewers = ''
            patchset = 'created (%s)' % self.patchset_size_str(data)
        else:
            reviewers = ','.join(self.get_reviewers(data))
            if reviewers:
                reviewers += ': '
            if data['patchSet']['kind'] == 'TRIVIAL_REBASE':
                patchset = 'rebased patchset %s of' % data['patchSet']['number']
            else:
                patchset = 'added patchset %s to' % data['patchSet']['number']
        msg = '%s%s %s %s' % (reviewers,
                              data['patchSet']['uploader'][Gerrit.SOURCE_USER_ID],
                              patchset,
                              self.project_description(data))
        self.ircbot.send(channel, msg)

    def ref_updated(self, channel, data):
        refName = data['refUpdate']['refName']
        ref_re = re.match(r'refs/([^/]*)/(.*)', refName)
        msg = None

        if ref_re and ref_re.group(1) in ('tags', 'heads'):
            if ref_re.group(1) == 'tags':
                if data['refUpdate']['oldRev'] == Gerrit.EMPTY_GIT_HASH:
                    msg = 'tagged %s' % ref_re.group(2)
                elif data['refUpdate']['newRev'] == Gerrit.EMPTY_GIT_HASH:
                    msg = 'removed tag %s' % ref_re.group(2)
                else:
                    msg = 'updated tag %s' % ref_re.group(2)
            if ref_re.group(1) == 'heads':
                if data['refUpdate']['oldRev'] == Gerrit.EMPTY_GIT_HASH:
                    msg = 'created branch %s' % branch_name.group(2)
                else:
                    msg = 'force-updated branch %s' % branch_name.group(2)
        else:
            msg = 'touched ref %s' % refName

        if msg:
            msg = '%s %s on %s' % (
                data['submitter'][Gerrit.SOURCE_USER_ID],
                msg,
                data['refUpdate']['project']
            )
            self.ircbot.send(channel, msg)

    def approval_changed(self, channel, data):
        approval_list = []
        comment = ''
        if data['author']['username'] != 'jenkins':
            head_end = data['comment'].find('\n\n')
            if head_end > 0:
                comment = data['comment'][head_end + 1:].replace('\n', ' ')

        for approval in data.get('approvals', []):
            intvalue = int(approval['value'])
            absvalue = abs(intvalue)
            textvalue = (intvalue >= 0 and '+' or '') + str(intvalue)
            sign = intvalue >= 0 and 'plus' or 'minus'
            if approval['type'] == 'Verified':
                event = 'verified-%s-%d' % (sign, absvalue)
                if self.channel_has_event(event):
                    approval_list.append('Verified%s' % textvalue)
                if intvalue < 0 and data['comment'].endswith('ABORTED'):
                    approval_list.append('aborted')

            elif approval['type'] == 'Code-Review':
                if absvalue == 1:
                    sign = 'plus-minus'
                event = 'review-%s-%d' % (sign, absvalue)
                if self.channel_has_event(event):
                    approval_list.append('CR%s' % textvalue)

        if approval_list:
            msg = '%s: %s%s by %s on %s' % (data['change']['owner'][Gerrit.TARGET_USER_ID],
                                            ', '.join(approval_list),
                                            comment,
                                            data['author'][Gerrit.SOURCE_USER_ID],
                                            self.project_description(data))
            self.ircbot.send(channel, msg)

    def comment_added(self, channel, data):
        if data['author']['username'] == 'jenkins':
            return
        reviewers = self.get_reviewers(data)
        author = data['author']['username']
        if author == data['change']['owner']['username']:
            target = ','.join(reviewers)
        else:
            if author in reviewers:
                reviers.remove('author')
            reviewers.insert(0, data['change']['owner'][Gerrit.TARGET_USER_ID])
            target = ','.join(reviewers)
        if target:
            target += ': '
        comment = data['comment']
        comment_head_end = comment.find('\n\n')
        if comment_head_end >= 0:
            comment = comment[comment_head_end + 2:]
        comment = comment.replace('\n', ' ')
        msg = '%s%s commented %s on %s' % (
            target,
            data['author'][Gerrit.SOURCE_USER_ID],
            comment,
            self.project_description(data)
        )
        self.ircbot.send(channel, msg)

    def reviewer_added(self, channel, data):
        msg = '%s: %s invites you to review %s' % (
            data['reviewer'][Gerrit.TARGET_USER_ID],
            data['change']['owner'][Gerrit.SOURCE_USER_ID],
            self.project_description(data)
        )
        self.ircbot.send(channel, msg)

    def change_merged(self, channel, data):
        msg = '%s merged %s' % (data['submitter'][Gerrit.SOURCE_USER_ID],
                                self.project_description(data))
        self.ircbot.send(channel, msg)

    def change_abandoned(self, channel, data):
        if data.has_key('reason') and data['reason']:
            reason = ': ' + data['reason']
        else:
            reason = ''
        msg = '%s abandoned %s%s' % (data['abandoner'][Gerrit.SOURCE_USER_ID],
                                     self.project_description(data),
                                     reason)
        self.ircbot.send(channel, msg)

    def change_restored(self, channel, data):
        reason = data['reason']
        if reason:
            reason = ': ' + reason
        msg = '%s restored %s%s' % (data['restorer'][Gerrit.SOURCE_USER_ID],
                                    self.project_description(data),
                                    reason)
        self.ircbot.send(channel, msg)

    def _read(self, data):
        event = None
        try:
            event = data['type']
            if event == 'reviewer-added':
                self.add_reviewer(data)
            elif event in ['change-merged', 'change-abandoned']:
                self.clear_reviewers(data)

            channel_set = (self.channel_config.events.get(Gerrit.WILDCARD, set()) |
                           self.channel_config.events.get(event, set()))
            if event != 'ref-updated':
                projects = (self.channel_config.projects.get(Gerrit.WILDCARD, set()) |
                            self.channel_config.projects.get(
                                            data['change']['project'], set()))
                branches = (self.channel_config.branches.get(Gerrit.WILDCARD, set()) |
                            self.channel_config.branches.get(
                                            data['change']['branch'], set()))
                channel_set &= projects & branches
        except KeyError:
            # The data we care about was not present, no channels want
            # this event.
            pass

        if not channel_set:
            channel_set = set()
        self.log.info('Potential channels to receive event notification: %s' %
                      channel_set)
        for channel in channel_set:
            if event == 'comment-added':
                if data.has_key('approvals'):
                    self.approval_changed(channel, data)
                else:
                    self.comment_added(channel, data)
            elif event == 'patchset-created':
                self.patchset_created(channel, data)
            elif event == 'change-merged':
                self.change_merged(channel, data)
            elif event == 'change-abandoned':
                self.change_abandoned(channel, data)
            elif event == 'change-restored':
                self.change_restored(channel, data)
            elif event == 'ref-updated':
                self.ref_updated(channel, data)
            elif event == 'reviewer-added':
                self.reviewer_added(channel, data)

    def run(self):
        while True:
            while not self.connected:
                self.connect()
            try:
                event = self.gerrit.getEvent()
                self._read(event)
            except Exception:
                self.log.exception('Exception encountered in event loop')
                if not self.gerrit.watcher_thread.is_alive():
                    # Start new gerrit connection. Don't need to restart IRC
                    # bot, it will reconnect on its own.
                    self.connected = False


class GerritMQTT(Gerrit):
    def __init__(self, ircbot, channel_config, server, base_topic='gerrit',
                 port=1883, websocket=False):
        threading.Thread.__init__(self)
        self.ircbot = ircbot
        self.channel_config = channel_config
        self.log = logging.getLogger('gerritbot')
        self.server = server
        self.port = port
        self.websocket = websocket
        self.base_topic = base_topic
        self.connected = False

    def connect(self):
        try:
            self.client.connect(self.server, port=self.port)

            self.log.info('Start watching Gerrit event stream via mqtt!.')
            self.connected = True
        except Exception:
            self.log.exception('Exception while connecting to mqtt')
            self.client.reinitialise()
            self.connected = False
            # Delay before attempting again.
            time.sleep(1)

    def run(self):
        def _on_connect(client, userdata, flags, rc):
            client.subscribe(self.base_topic + '/#')

        def _on_message(client, userdata, msg):
            data = json.loads(msg.payload)
            if data:
                self._read(data)

        if self.websocket:
            self.client = mqtt.Client(transport='websockets')
        else:
            self.client = mqtt.Client()
        self.client.on_connect = _on_connect
        self.client.on_message = _on_message

        while True:
            while not self.connected:
                self.connect()
            try:
                self.client.loop()
            except Exception:
                self.log.exception('Exception encountered in event loop')
                time.sleep(5)


class ChannelConfig(object):
    def __init__(self, data):
        self.data = data
        keys = data.keys()
        for key in keys:
            if key[0] != '#':
                data['#' + key] = data.pop(key)
        self.channels = data.keys()
        self.projects = {}
        self.events = {}
        self.branches = {}
        for channel, val in iter(self.data.items()):
            for event in val['events']:
                event_set = self.events.get(event, set())
                event_set.add(channel)
                self.events[event] = event_set
            for project in val['projects']:
                project_set = self.projects.get(project, set())
                project_set.add(channel)
                self.projects[project] = project_set
            for branch in val['branches']:
                branch_set = self.branches.get(branch, set())
                branch_set.add(channel)
                self.branches[branch] = branch_set


def _main(config):
    setup_logging(config)

    fp = config.get('ircbot', 'channel_config')
    if fp:
        fp = os.path.expanduser(fp)
        if not os.path.exists(fp):
            raise Exception("Unable to read layout config file at %s" % fp)
    else:
        raise Exception("Channel Config must be specified in config file.")

    try:
        channel_config = ChannelConfig(yaml.load(open(fp)))
    except Exception:
        log = logging.getLogger('gerritbot')
        log.exception("Syntax error in chanel config file")
        raise

    bot = GerritBot(channel_config.channels,
                    config.get('ircbot', 'nick'),
                    config.get('ircbot', 'pass'),
                    config.get('ircbot', 'server'),
                    config.getint('ircbot', 'port'),
                    config.getboolean('ircbot', 'force_ssl'),
                    config.get('ircbot', 'server_password'))
    if config.has_option('ircbot', 'use_mqtt'):
        use_mqtt = config.getboolean('ircbot', 'use_mqtt')
    else:
        use_mqtt = False

    if use_mqtt:
        g = GerritMQTT(bot,
                       channel_config,
                       config.get('mqtt', 'host'),
                       config.get('mqtt', 'base_topic'),
                       config.getint('mqtt', 'port'),
                       config.getboolean('mqtt', 'websocket'))
    else:
        g = Gerrit(bot,
                   channel_config,
                   config.get('gerrit', 'host'),
                   config.get('gerrit', 'user'),
                   config.getint('gerrit', 'port'),
                   config.get('gerrit', 'key'))
    g.start()
    bot.start()


def main():
    if len(sys.argv) != 2:
        print("Usage: %s CONFIGFILE" % sys.argv[0])
        sys.exit(1)

    config = ConfigParser.ConfigParser({'force_ssl': 'false',
                                        'server_password': None})
    config.read(sys.argv[1])

    pid_path = ""
    if config.has_option('ircbot', 'pid'):
        pid_path = config.get('ircbot', 'pid')
    else:
        pid_path = "/var/run/gerritbot/gerritbot.pid"

    pid = pid_file_module.TimeoutPIDLockFile(pid_path, 10)
    #with daemon.DaemonContext(pidfile=pid, working_directory=os.getcwd()):
    #    _main(config)
    _main(config)


def setup_logging(config):
    if config.has_option('ircbot', 'log_config'):
        log_config = config.get('ircbot', 'log_config')
        fp = os.path.expanduser(log_config)
        if not os.path.exists(fp):
            raise Exception("Unable to read logging config file at %s" % fp)
        logging.config.fileConfig(fp)
    else:
        logging.basicConfig(level=logging.DEBUG)


if __name__ == "__main__":
    main()
