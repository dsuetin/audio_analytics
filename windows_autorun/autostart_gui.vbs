' autostart_gui.vbs
' Launches the status window (GUI) with the BASE (system) pythonw.exe directly
' from the Python instance that .venv was created from (the "home =" line in
' pyvenv.cfg). The venv's site-packages are exposed through PYTHONPATH so all
' dependencies are loaded from the virtual environment. This way only ONE
' process appears per GUI instance in Task Manager (no venv-launcher wrapper).
' Launched hidden (window style 0) so no terminal window appears.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
installDir = fso.GetParentFolderName(WScript.ScriptFullName)
venvDir = installDir & "\.venv"

' 1) Locate the base interpreter directory from pyvenv.cfg ("home = <dir>").
baseDir = ""
cfgPath = venvDir & "\pyvenv.cfg"
If fso.FileExists(cfgPath) Then
    Set ts = fso.OpenTextFile(cfgPath, 1)
    Do While Not ts.AtEndOfStream
        line = Trim(ts.ReadLine)
        If InStr(1, line, "home =", 1) = 1 Then
            baseDir = Trim(Mid(line, 7))
            Exit Do
        End If
    Loop
    ts.Close
End If

If baseDir = "" Then
    WScript.Quit 1
End If

py = baseDir & "\pythonw.exe"
If Not fso.FileExists(py) Then
    WScript.Quit 1
End If

' 2) Make venv site-packages visible to the base interpreter.
sitePackages = venvDir & "\Lib\site-packages"
If fso.FolderExists(sitePackages) Then
    sh.Environment("PROCESS")("PYTHONPATH") = sitePackages
End If

sh.Run """" & py & """ """ & installDir & "\gui.py""", 0, False
