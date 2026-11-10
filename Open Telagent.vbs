Option Explicit
Dim shell, files, folder, python, script
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")
script = files.BuildPath(folder, "gui.py")
If Not files.FileExists(python) Then
    MsgBox "The Python environment is missing. Please complete the setup steps in README.md.", 48, "Telagent"
    WScript.Quit 1
End If
shell.CurrentDirectory = folder
shell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34), 1, False
