Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$ws = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
foreach ($w in $ws) {
  $n = $w.Current.Name
  if ($n -match 'MarginMise|Login|Password|Sign|Manager') {
    Write-Output "WINDOW: $n"
    $cs = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($c in $cs) {
      $name = $c.Current.Name
      $type = $c.Current.ControlType.ProgrammaticName
      $aid = $c.Current.AutomationId
      if ($name -or $type) { Write-Output "  $type | Name=$name | Id=$aid" }
    }
  }
}
