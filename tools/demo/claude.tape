# Records the raw Claude Code demo; record.sh crops the start banner (version, plan, folder)
# out of the final docs/assets/demo-claude.gif. @MCP_CONFIG@ points at `qlib-sync mcp`.
Output "@RAW_GIF@"

Set Shell bash
Set Theme "GitHub Dark"
Set FontSize 15
Set Width 1240
Set Height 1100
Set Framerate 24
Set PlaybackSpeed 1.5
Set TypingSpeed 30ms

Env BASH_SILENCE_DEPRECATION_WARNING "1"

Hide
Type "for v in $(env | grep -o '^CLAUDE[A-Z_]*'); do unset $v; done; export PS1='$ ' && cd @DEMO_DIR@ && clear"
Enter
Type "claude --mcp-config @MCP_CONFIG@ --strict-mcp-config --permission-mode default --allowedTools 'mcp__qlib-sync__cn_status' 'mcp__qlib-sync__cn_get_prices' 'mcp__qlib-sync__cn_list_pool'"
Enter
Sleep 9s
Show

Sleep 1s
Type "Is my A-share data up to date? Show Kweichow Moutai's closing prices for the last 5 trading days, and how many stocks are in the CSI 300 today."
Sleep 500ms
Enter
Sleep 75s
