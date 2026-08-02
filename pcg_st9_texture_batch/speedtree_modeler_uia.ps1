param(
    [Parameter(Mandatory = $true)][int]$OwnedProcessId,
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$DocumentName,
    [Parameter(Mandatory = $true)][ValidateSet("save", "close")][string]$Operation,
    [Parameter(Mandatory = $true)][int]$TimeoutSeconds
)

$ErrorActionPreference = "Stop"
$Contract = "speedtree_modeler_owned_semantic_uia_v1"

function Throw-Reason([string]$Token, [string]$Message) {
    throw [System.InvalidOperationException]::new($Token + "|" + $Message)
}

function Find-ProcessElements {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $OwnedProcessId
    )
    return [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        $condition
    )
}

function Wait-ExactElement(
    [string]$AccessibleName,
    [string[]]$AllowedControlTypes,
    [string]$MissingToken,
    [string]$AmbiguousToken
) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $matches = @(
            Find-ProcessElements | Where-Object {
                $_.Current.Name -ceq $AccessibleName -and
                $AllowedControlTypes -contains $_.Current.ControlType.ProgrammaticName -and
                $_.Current.IsEnabled
            }
        )
        if ($matches.Count -gt 1) {
            Throw-Reason $AmbiguousToken "multiple exact accessible elements matched"
        }
        if ($matches.Count -eq 1) {
            return $matches[0]
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    Throw-Reason $MissingToken "the exact accessible element did not appear"
}

function Invoke-ExactMenu([string]$AccessibleName, [string]$MissingToken) {
    $parameters = @{
        AccessibleName = $AccessibleName
        AllowedControlTypes = @("ControlType.MenuItem")
        MissingToken = $MissingToken
        AmbiguousToken = "uia_menu_ambiguous"
    }
    $element = Wait-ExactElement @parameters
    $patternObject = $null
    if (-not $element.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [ref]$patternObject
    )) {
        Throw-Reason "uia_invoke_pattern_missing" "the exact menu item has no InvokePattern"
    }
    ([System.Windows.Automation.InvokePattern]$patternObject).Invoke()
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes

    $process = Get-Process -Id $OwnedProcessId -ErrorAction Stop
    $expectedExecutable = [System.IO.Path]::GetFullPath($ExecutablePath)
    $actualExecutable = [System.IO.Path]::GetFullPath($process.MainModule.FileName)
    if (-not [string]::Equals(
        $expectedExecutable,
        $actualExecutable,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        Throw-Reason "uia_owned_executable_mismatch" "the owned PID executable differs"
    }
    if ([System.IO.Path]::GetFileName($DocumentName) -cne $DocumentName -or
        [System.IO.Path]::GetExtension($DocumentName) -ine ".spm") {
        Throw-Reason "uia_document_identity_invalid" "the document name is not one exact SPM basename"
    }

    $documentParameters = @{
        AccessibleName = $DocumentName
        AllowedControlTypes = @(
            "ControlType.Document",
            "ControlType.TabItem",
            "ControlType.Window",
            "ControlType.Pane"
        )
        MissingToken = "uia_document_missing"
        AmbiguousToken = "uia_document_ambiguous"
    }
    $document = Wait-ExactElement @documentParameters
    if ($document.Current.ProcessId -ne $OwnedProcessId) {
        Throw-Reason "uia_document_pid_mismatch" "the document is not owned by the exact PID"
    }
    $document.SetFocus()

    Invoke-ExactMenu "File" "uia_file_menu_missing"
    $actionName = if ($Operation -eq "save") { "Save" } else { "Close" }
    Invoke-ExactMenu $actionName ("uia_" + $Operation + "_menu_missing")

    if ($Operation -eq "close") {
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        do {
            $remaining = @(
                Find-ProcessElements | Where-Object {
                    $_.Current.Name -ceq $DocumentName -and
                    @(
                        "ControlType.Document",
                        "ControlType.TabItem",
                        "ControlType.Window",
                        "ControlType.Pane"
                    ) -contains $_.Current.ControlType.ProgrammaticName
                }
            )
            if ($remaining.Count -eq 0) { break }
            if ($remaining.Count -gt 1) {
                Throw-Reason "uia_document_ambiguous" "document identity became ambiguous during close"
            }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $deadline)
        if ($remaining.Count -ne 0) {
            Throw-Reason "uia_exact_document_close_unverified" "the exact document remained visible"
        }
    }

    [ordered]@{
        ok = $true
        contract = $Contract
        owned_process_id = $OwnedProcessId
        document_accessible_name = $DocumentName
        operation = $Operation
        menu_path = @("File", $actionName)
        semantic_pattern = "InvokePattern"
    } | ConvertTo-Json -Compress
    exit 0
}
catch {
    $rawMessage = [string]$_.Exception.Message
    $parts = $rawMessage.Split(@("|"), 2, [System.StringSplitOptions]::None)
    $reason = if ($parts.Count -eq 2) { $parts[0] } else { "uia_semantic_invoke_failed" }
    [ordered]@{
        ok = $false
        contract = $Contract
        reason_token = $reason
        owned_process_id = $OwnedProcessId
        document_accessible_name = $DocumentName
        operation = $Operation
    } | ConvertTo-Json -Compress
    exit 1
}
