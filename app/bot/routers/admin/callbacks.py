"""Admin callback-data constants."""
from __future__ import annotations

PREFIX = "adm"

NOP = f"{PREFIX}:nop"

# main
MENU = f"{PREFIX}:menu"
CLOSE = f"{PREFIX}:close"
USER_MENU = f"{PREFIX}:usermenu"

# users
USERS = f"{PREFIX}:users"
USER_PAGE = f"{PREFIX}:users:p:"
USER_OPEN = f"{PREFIX}:u:open:"
USER_TOGGLE_BLOCK = f"{PREFIX}:u:block:"
USER_NOTIFY = f"{PREFIX}:u:msg:"
USER_NOTIFY_CANCEL = f"{PREFIX}:u:msgcancel:"
USER_DEL_ACC_CONFIRM = f"{PREFIX}:u:del:"
USER_DEL_ACC_OK = f"{PREFIX}:u:delok:"

# channels (mandatory subscription)
CH_SUBSCRIPTION = f"{PREFIX}:ch"
CH_TAB_CHANNELS = f"{PREFIX}:ch:tab:channels"
CH_TAB_GROUPS = f"{PREFIX}:ch:tab:groups"
CH_ADD_CHANNEL = f"{PREFIX}:ch:add:channel"
CH_ADD_GROUP = f"{PREFIX}:ch:add:group"
CH_TOGGLE = f"{PREFIX}:ch:tg:"
CH_DELETE = f"{PREFIX}:ch:del:"
CH_DELETE_OK = f"{PREFIX}:ch:delok:"

# stats
STATS = f"{PREFIX}:stats"

# broadcast
BCAST = f"{PREFIX}:bcast"
BCAST_PAGE = f"{PREFIX}:bcast:page:"
BCAST_COMPOSE = f"{PREFIX}:bcast:compose"
BCAST_SEND = f"{PREFIX}:bcast:send"
BCAST_NEW = f"{PREFIX}:bcast:new"
BCAST_DRAFT_RESUME = f"{PREFIX}:bcast:draft:"
BCAST_TARGET_X = f"{PREFIX}:bcast:target:"
BCAST_DRY_RUN = f"{PREFIX}:bcast:dry_run"
BCAST_TEST_SEND = f"{PREFIX}:bcast:test_send"
BCAST_SEND_NOW = f"{PREFIX}:bcast:send_now"
BCAST_SCHEDULE = f"{PREFIX}:bcast:schedule"
BCAST_CANCEL_LIVE = f"{PREFIX}:bcast:cancel:"
BCAST_PAUSE_LIVE = f"{PREFIX}:bcast:pause:"
BCAST_RESUME_LIVE = f"{PREFIX}:bcast:resume:"
BCAST_HISTORY = f"{PREFIX}:bcast:history"
BCAST_VIEW = f"{PREFIX}:bcast:view:"

# settings
SETTINGS = f"{PREFIX}:settings"
SET_LANG = f"{PREFIX}:set:lang"
SET_LANG_AR = f"{PREFIX}:set:lang:ar"
SET_LANG_EN = f"{PREFIX}:set:lang:en"

# notifications
NOTIFY = f"{PREFIX}:notify"
NOTIFY_PAGE = f"{PREFIX}:notify:p:"
NOTIFY_READ = f"{PREFIX}:notify:read:"
NOTIFY_DISMISS = f"{PREFIX}:notify:dismiss:"
NOTIFY_MARK_ALL = f"{PREFIX}:notify:markall"
NOTIFY_TOGGLE = f"{PREFIX}:notify:toggle:"

# backups
BAK = f"{PREFIX}:bak"
BAK_NEW = f"{PREFIX}:bak:new"
BAK_OPEN = f"{PREFIX}:bak:v:"

# search
SEARCH = f"{PREFIX}:search"
SEARCH_PAGE = f"{PREFIX}:search:p:"
SEARCH_RESULTS = f"{PREFIX}:search:results:"
