# Records docs/assets/demo-update.gif at 2x speed (the real download takes about a minute).
# Before recording, install an older release into the
# demo HOME (see tools/demo/README.md) so the recording shows a real update.
Output docs/assets/demo-update.gif

Set Shell bash
Set Theme "GitHub Dark"
Set FontSize 14
Set Width 1320
Set Height 820
Set Framerate 24
Set PlaybackSpeed 2
Set Padding 24
Set TypingSpeed 40ms
Set WaitTimeout 180s

Env BASH_SILENCE_DEPRECATION_WARNING "1"

Hide
Type "export HOME=@DEMO_HOME@ PATH=@DEMO_PATH@ PS1='$ ' && cd ~ && clear"
Enter
Sleep 500ms
Show

Type "qlib-sync cn status | head -4"
Sleep 300ms
Enter
Wait+Screen /Community release/
Sleep 2.5s

Type "qlib-sync cn update"
Sleep 300ms
Enter
Wait+Screen /Delisted symbols/
Sleep 3s

Type "qlib-sync cn update"
Sleep 300ms
Enter
Wait+Screen /Already up to date/
Sleep 4s
