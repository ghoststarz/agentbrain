' AgentBrain memory sync launcher.
' Runs memory-sync.ps1 with a fully hidden window.
' Task Scheduler started with powershell.exe ALWAYS shows a console window,
' even with -WindowStyle Hidden; wscript + Run(...,0,...) does not.
Option Explicit

Dim fso, shell, base, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & base & "\memory-sync.ps1"""

' 0 = hidden window, True = wait for completion
shell.Run cmd, 0, True
