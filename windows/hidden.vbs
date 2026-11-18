' Runs a command with no console window. Used by the scheduled tasks.
Set sh = CreateObject("WScript.Shell")
sh.Run """" & WScript.Arguments(0) & """", 0, False
