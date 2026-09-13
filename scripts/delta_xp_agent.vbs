' ================================================================================
' Microinvest Delta Pro - Windows XP In-VM Companion Agent
' ================================================================================
' Този скрипт се изпълнява вътре в Windows XP виртуалната машина в UTM.
' Той следи за нови партиди от OCR пайплайна (на диск E:\ или Z:\),
' проверява активната фирма в Delta Pro и подпомага автоматизирания импорт/експорт.
' ================================================================================

Option Explicit

Dim fso, wshShell, strUsbPath, strSharedPath, strActivePath
Set fso = CreateObject("Scripting.FileSystemObject")
Set wshShell = CreateObject("WScript.Shell")

strUsbPath = "E:\TRANSFER.LOG"
strSharedPath = "Z:\206062202_Building_11\TRANSFER.LOG"

WScript.Echo "=========================================================="
WScript.Echo " MICROINVEST DELTA PRO - COMPANION AGENT (WINDOWS XP)"
WScript.Echo "=========================================================="
WScript.Echo "Следене за входящи TRANSFER.LOG пакети..."

If fso.FileExists(strUsbPath) Then
    strActivePath = strUsbPath
    WScript.Echo "[OK] Открит е пакет на флашка (USB): " & strUsbPath
ElseIf fso.FileExists(strSharedPath) Then
    strActivePath = strSharedPath
    WScript.Echo "[OK] Открит е пакет в споделена папка: " & strSharedPath
ElseIf fso.FileExists("C:\TRANSFER.LOG") Then
    strActivePath = "C:\TRANSFER.LOG"
    WScript.Echo "[OK] Открит е пакет в C:\TRANSFER.LOG"
Else
    WScript.Echo "[INFO] Все още няма нов TRANSFER.LOG."
    WScript.Echo "Моля, натиснете 'Прехвърли към WinXP VM' в уеб таблото."
    WScript.Quit 0
End If

Dim logFile, sizeKb
Set logFile = fso.GetFile(strActivePath)
sizeKb = Round(logFile.Size / 1024, 1)

WScript.Echo "Размер на файла: " & sizeKb & " KB"
WScript.Echo "Последна промяна: " & logFile.DateLastModified

' Focus Microinvest Delta window if open
If wshShell.AppActivate("Microinvest Делта") Then
    WScript.Echo "[OK] Прозорецът 'Microinvest Делта' е активен."
    WScript.Echo ""
    WScript.Echo "УКАЗАНИЯ ЗА ИМПОРТ В ДЕЛТА PRO:"
    WScript.Echo "1. Уверете се, че в статус бара пише 'БИЛДИНГ 11'."
    WScript.Echo "2. Натиснете 'Обмен' -> 'Импорт на операции...' (или Alt+O -> I)."
    WScript.Echo "3. Посочете файла: " & strActivePath
    WScript.Echo "4. Потвърдете импорта."
Else
    WScript.Echo "[ВНИМАНИЕ] Програмата 'Microinvest Делта' не е отворена."
    WScript.Echo "Моля стартирайте я и отворете фирма 'БИЛДИНГ 11'."
End If

WScript.Echo "=========================================================="
