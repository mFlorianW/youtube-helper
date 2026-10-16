# youtube-helper

Keeps a Jellyfin media library stocked with new videos from the YouTube channels the owner is subscribed to.

## Language

**Subscription**:
A YouTube channel the owner is subscribed to on their own Google account. The account is the source of truth; there is no separately maintained channel list.
_Avoid_: Channel list, followed channel, watchlist

**New upload**:
A regular video (premieres included once aired; Shorts and livestreams excluded) a Subscription publishes after the tool started tracking that Subscription. Tracking restarts when the owner re-subscribes. Anything published before the current tracking start is back catalogue and is never fetched.
_Avoid_: Latest video, recent video

**Media library**:
The directory, specified by the admin, into which downloaded videos are placed. A media server such as Jellyfin may scan it, but the tool knows nothing about that media server.
_Avoid_: Download folder, output dir, Jellyfin folder

**Channel folder**:
The folder inside the Media library that holds one Subscription's videos. Its name is taken from the channel's display name when the Subscription is first seen and never changes afterwards, even if the channel is renamed.
_Avoid_: Show folder, series folder
