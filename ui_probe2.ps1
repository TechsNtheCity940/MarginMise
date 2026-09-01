Add-Type -AssemblyName UIAutomationClient
$root=[System.Windows.Automation.AutomationElement]::RootElement
$all=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
foreach($e in $all){
  try {
    if($e.Current.Name -match 'Sign in|Barrel|password|Password|username|Username') {
      Write-Output "Name=$($e.Current.Name) Type=$($e.Current.ControlType.ProgrammaticName) HWND=$($e.Current.NativeWindowHandle) Id=$($e.Current.AutomationId)"
    }
  } catch {}
}
