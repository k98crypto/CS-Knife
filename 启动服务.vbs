' Launch both services by double-click.
'   1) relay  : visible console window (so you can read logs / close it to stop)
'   2) HUD    : hidden (tray-less overlay, use its own close button)
' ASCII-only on purpose: avoids any VBScript encoding issues.

Set ws  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = here

If Not fso.FileExists(here & "\bridge_server.py") Then
    MsgBox "bridge_server.py not found. Put this file in the project root.", 16, "Launch"
    WScript.Quit 1
End If

' relay (window title is used by launcher.bat / cleanup scripts)
ws.Run "cmd /c title CS-WebSocket & python bridge_server.py", 1, False

WScript.Sleep 2000

If fso.FileExists(here & "\semi_runner.pyw") Then
    ws.Run "pythonw semi_runner.pyw", 0, False
End If
