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
CHANNELS = f"{PREFIX}:channels"
CHANNEL_ADD = f"{PREFIX}:ch:add"
CHANNEL_TOGGLE = f"{PREFIX}:ch:tg:"
CHANNEL_DEL = f"{PREFIX}:ch:del:"
CHANNEL_DEL_OK = f"{PREFIX}:ch:delok:"

# stats
STATS = f"{PREFIX}:stats"

# broadcast
BCAST = f"{PREFIX}:bcast"
BCAST_PAGE = f"{PREFIX}:bcast:page:"
BCAST_COMPOSE = f"{PREFIX}:bcast:compose"
BCAST_SEND = f"{PREFIX}:bcast:send"

# settings
SETTINGS = f"{PREFIX}:settings"
SET_LANG = f"{PREFIX}:set:lang"
SET_LANG_AR = f"{PREFIX}:set:lang:ar"
SET_LANG_EN = f"{PREFIX}:set:lang:en"

# backups
BAK = f"{PREFIX}:bak"
BAK_NEW = f"{PREFIX}:bak:new"
BAK_OPEN = f"{PREFIX}:bak:v:"

# search
SEARCH = f"{PREFIX}:search"
SEARCH_PAGE = f"{PREFIX}:search:p:"
SEARCH_RESULTS = f"{PREFIX}:search:results:"
