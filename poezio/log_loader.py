"""
This modules contains a class that loads messages into a ChatTab, either from
MAM or the local logs, and a class that loads MUC history into the local
logs.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List
from poezio import tabs
from poezio.logger import (
    build_log_message,
    iterate_messages_reverse,
    last_message_in_archive,
    Logger,
    LogDict,
)
from poezio.mam import (
    fetch_history,
    NoMAMSupportException,
    MAMQueryException,
    DiscoInfoException,
    make_line,
)
from poezio.common import to_utc
from poezio.ui.types import EndOfArchive, Message, BaseMessage
from poezio.text_buffer import HistoryGap
from slixmpp import JID


log = logging.getLogger(__name__)


def make_line_local(tab: tabs.ChatTab, msg: LogDict) -> Message:
    """Create a UI message from a local log read.

    :param tab: Tab in which that message will be displayed
    :param msg: Log data
    :returns: The UI message
    """
    if isinstance(tab, tabs.MucTab):
        jid = JID(tab.jid)
        jid.resource = msg.get('nickname') or ''
    else:
        jid = JID(tab.jid)
    msg['time'] = msg['time'].astimezone(tz=timezone.utc)
    return make_line(tab, msg['txt'], msg['time'], jid, '', msg['nickname'])


class LogLoader:
    """
    An ephemeral class that loads history in a tab.

    Loading from local logs is blocked until history has been fetched from
    MAM to fill the local archive.
    """
    logger: Logger
    tab: tabs.ChatTab
    mam_only: bool

    def __init__(self, logger: Logger, tab: tabs.ChatTab,
                 local_logs: bool = True):
        self.mam_only = not local_logs
        self.logger = logger
        self.tab = tab

    async def tab_open(self) -> None:
        """Called on a tab opening or a MUC join"""
        amount = 2 * self.tab.text_win.height
        gap = self.tab._text_buffer.find_last_gap_muc()
        messages = []
        if gap is not None:
            if self.mam_only:
                messages = await self.mam_fill_gap(gap)
            else:
                messages = await self.local_fill_gap(gap)
        else:
            if self.mam_only:
                messages = await self.mam_tab_open(amount)
            else:
                messages = await self.local_tab_open(amount)

        log.debug(
            'Fetched %s messages for %s',
            len(messages), self.tab.jid
        )
        if messages:
            self.tab._text_buffer.add_history_messages(messages)
            self.tab.core.refresh_window()

    async def mam_tab_open(self, nb: int) -> List[BaseMessage]:
        """Fetch messages in MAM when opening a new tab.

        :param nb: number of max messages to fetch.
        :returns: list of ui messages to add
        """
        tab = self.tab
        end = datetime.now()
        for message in tab._text_buffer.messages:
            time_ok = to_utc(message.time) < to_utc(end)
            if isinstance(message, Message) and time_ok:
                end = message.time
                break
        end = end - timedelta(microseconds=1)
        try:
            return await fetch_history(tab, end=end, amount=nb)
        except (NoMAMSupportException, MAMQueryException, DiscoInfoException):
            return []
        finally:
            tab.query_status = False

    async def local_tab_open(self, nb: int) -> List[BaseMessage]:
        """Fetch messages locally when opening a new tab.

        :param nb: number of max messages to fetch.
        :returns: list of ui messages to add
        """
        await self.wait_mam()
        results: List[BaseMessage] = []
        filepath = self.logger.get_file_path(self.tab.jid)
        for msg in iterate_messages_reverse(filepath):
            typ_ = msg.pop('type')
            if typ_ == 'message':
                results.append(make_line_local(self.tab, msg))
            if len(results) >= nb:
                break
        return results[::-1]

    async def mam_fill_gap(self, gap: HistoryGap) -> List[BaseMessage]:
        """Fill a message gap in an existing tab using MAM.

        :param gap: Object describing the history gap
        :returns: list of ui messages to add
        """
        tab = self.tab

        start = gap.last_timestamp_before_leave
        end = gap.first_timestamp_after_join
        if start:
            start = start + timedelta(seconds=1)
        if end:
            end = end - timedelta(seconds=1)
        try:
            return await fetch_history(tab, start=start, end=end, amount=999)
        except (NoMAMSupportException, MAMQueryException, DiscoInfoException):
            return []
        finally:
            tab.query_status = False

    async def local_fill_gap(self, gap: HistoryGap) -> List[BaseMessage]:
        """Fill a message gap in an existing tab using the local logs.
        Mostly useless when not used with the MAMFiller.

        :param gap: Object describing the history gap
        :returns: list of ui messages to add
        """
        await self.wait_mam()
        start = gap.last_timestamp_before_leave
        end = gap.first_timestamp_after_join

        results: List[BaseMessage] = []
        filepath = self.logger.get_file_path(self.tab.jid)
        for msg in iterate_messages_reverse(filepath):
            typ_ = msg.pop('type')
            if start and msg['time'] < start:
                break
            if typ_ == 'message' and (not end or msg['time'] < end):
                results.append(make_line_local(self.tab, msg))
        return results[::-1]

    async def scroll_requested(self):
        """When a scroll up is requested in a chat tab.

        Try to load more history if there are no more messages in the buffer.
        """
        tab = self.tab
        tw = tab.text_win

        # If position in the tab is < two screen pages, then fetch MAM, so that
        # wa keep some prefetched margin. A first page should also be
        # prefetched on join if not already available.
        total, pos, height = len(tw.built_lines), tw.pos, tw.height
        rest = (total - pos) // height

        if rest > 1:
            return None

        if self.mam_only:
            messages = await self.mam_scroll_requested(height)
        else:
            messages = await self.local_scroll_requested(height)
        if messages:
            tab._text_buffer.add_history_messages(messages)
            tab.core.refresh_window()

    async def local_scroll_requested(self, nb: int) -> List[BaseMessage]:
        """Fetch messages locally on scroll up.

        :param nb: Number of messages to fetch
        :returns: list of ui messages to add
        """
        await self.wait_mam()
        tab = self.tab
        last_message_time = None
        if tab._text_buffer.messages:
            last_message_time = to_utc(tab._text_buffer.messages[0].time)
            last_message_time -= timedelta(microseconds=1)

        results: List[BaseMessage] = []
        filepath = self.logger.get_file_path(self.tab.jid)
        for msg in iterate_messages_reverse(filepath):
            typ_ = msg.pop('type')
            if last_message_time is None or msg['time'] < last_message_time:
                if typ_ == 'message':
                    results.append(make_line_local(self.tab, msg))
            if len(results) >= nb:
                break
        return results[::-1]

    async def mam_scroll_requested(self, nb: int) -> List[BaseMessage]:
        """Fetch messages from MAM on scroll up.

        :param nb: Number of messages to fetch
        :returns: list of ui messages to add
        """
        tab = self.tab
        try:
            messages = await fetch_history(tab, amount=nb)
            last_message_exists = False
            if tab._text_buffer.messages:
                last_message = tab._text_buffer.messages[0]
                last_message_exists = True
            if (not messages and
                    last_message_exists
                    and not isinstance(last_message, EndOfArchive)):
                time = tab._text_buffer.messages[0].time
                messages = [EndOfArchive('End of archive reached', time=time)]
            return messages
        except NoMAMSupportException:
            return []
        except (MAMQueryException, DiscoInfoException):
            tab.core.information(
                f'An error occured when fetching MAM for {tab.jid}',
                'Error'
            )
            return []
        finally:
            tab.query_status = False

    async def wait_mam(self) -> None:
        """Waitt for the MAM history sync before reading the local logs.

        Does nothing apart from blocking.
        """
        if self.tab.mam_filler is None:
            return
        await self.tab.mam_filler.done.wait()


class MAMFiller:
    """Class that loads messages from MAM history into the local logs.
    """
    tab: tabs.ChatTab
    logger: Logger
    future: asyncio.Future
    done: asyncio.Event
    max_msgs: int = 2000

    def __init__(self, logger: Logger, tab: tabs.ChatTab):
        self.tab = tab
        self.logger = logger
        logger.fd_busy(tab.jid)
        self.future = asyncio.ensure_future(self.fetch_routine())
        self.done = asyncio.Event()

    def cancel(self) -> None:
        """Cancel the routine and signal the end."""
        self.future.cancel()
        self.end()

    async def fetch_routine(self) -> None:
        """Load logs into the local archive, if possible."""
        filepath = self.logger.get_file_path(self.tab.jid)
        log.debug('Fetching logs for %s', self.tab.jid)
        try:
            last_msg = last_message_in_archive(filepath)
            last_msg_time = None
            if last_msg:
                last_msg_time = last_msg['time'] + timedelta(seconds=1)
            try:
                messages = await fetch_history(
                    self.tab,
                    start=last_msg_time,
                    amount=self.max_msgs,
                )
                log.debug(
                    'Fetched %s messages to fill local logs for %s',
                    len(messages), self.tab.jid,
                )
            except (DiscoInfoException, NoMAMSupportException, MAMQueryException):
                log.debug(
                    'Failed fetching logs for %s',
                    self.tab.jid, exc_info=True
                )
                return

            def build_message(msg) -> str:
                return build_log_message(
                    msg.nickname,
                    msg.txt,
                    msg.time,
                    prefix='MR',
                )

            logs = ''.join(map(build_message, messages))
            self.logger.log_raw(self.tab.jid, logs, force=True)
        finally:
            self.end()

    def end(self) -> None:
        """End a MAM fill (error or sucess). Remove references and signal on
        the Event().
        """
        try:
            self.logger.fd_available(self.tab.jid)
        except Exception:
            log.error('Error when restoring log fd:', exc_info=True)
        self.tab.mam_filler = None
        self.done.set()
