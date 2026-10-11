# =============================================================================
# OpenLDAP Remediation Demo - Control Panel (v2)
# =============================================================================
# A local Windows GUI that runs your existing vagrant/ssh commands for you.
#
# HOW TO RUN THIS FILE:
#   powershell -ExecutionPolicy Bypass -File .\control_panel.ps1
#
# v2 CHANGE: v1 used Process + Register-ObjectEvent for live output
# streaming, which is a known-flaky combination inside a WinForms message
# loop (events can queue up and never actually fire). v2 instead uses
# Start-Job + a polling Timer, which is the standard, reliable pattern for
# this in PowerShell - slightly less "instant" (updates every 0.5s instead
# of character-by-character) but it will actually work.
#
# WHAT THIS CANNOT DO (and why): this is a NATIVE Windows GUI, not a
# webpage - web pages run inside a browser sandbox and can never execute
# local commands like `vagrant` or `ssh`, on this or any computer.
# =============================================================================

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# --- EDIT THIS if your project folder ever moves ---
$ProjectPath = "C:\Users\HP\Desktop\openldap-lab\openldap-iam-demo (1)\openldap-iam-demo"

# =============================================================================
# GUI LAYOUT
# =============================================================================
$form = New-Object System.Windows.Forms.Form
$form.Text = "OpenLDAP Remediation Demo - Control Panel"
$form.Size = New-Object System.Drawing.Size(950, 700)
$form.StartPosition = "CenterScreen"
$form.MinimumSize = New-Object System.Drawing.Size(750, 500)

$pathLabel = New-Object System.Windows.Forms.Label
$pathLabel.Text = "Project: $ProjectPath"
$pathLabel.AutoSize = $false
$pathLabel.Dock = "Top"
$pathLabel.Height = 24
$pathLabel.Font = New-Object System.Drawing.Font("Segoe UI", 8, [System.Drawing.FontStyle]::Italic)
$form.Controls.Add($pathLabel)

$buttonPanel = New-Object System.Windows.Forms.FlowLayoutPanel
$buttonPanel.Dock = "Top"
$buttonPanel.AutoSize = $true
$buttonPanel.WrapContents = $true
$buttonPanel.Padding = New-Object System.Windows.Forms.Padding(5)
$form.Controls.Add($buttonPanel)

$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ScrollBars = "Vertical"
$logBox.ReadOnly = $true
$logBox.Dock = "Fill"
$logBox.Font = New-Object System.Drawing.Font("Consolas", 9)
$logBox.BackColor = [System.Drawing.Color]::Black
$logBox.ForeColor = [System.Drawing.Color]::LightGreen
$form.Controls.Add($logBox)
$logBox.BringToFront()

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "Ready."
$statusLabel.Dock = "Bottom"
$statusLabel.Height = 24
$statusLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($statusLabel)

# =============================================================================
# STATE
# =============================================================================
$script:currentJob = $null
$script:currentLabel = ""

function Append-Log {
    param([string]$Text)
    $logBox.AppendText("$Text`r`n")
    $logBox.SelectionStart = $logBox.Text.Length
    $logBox.ScrollToCaret()
}

function Set-Status {
    param([string]$Text)
    $statusLabel.Text = $Text
}

function Set-ButtonsEnabled {
    param([bool]$Enabled)
    foreach ($ctrl in $buttonPanel.Controls) {
        if ($ctrl -is [System.Windows.Forms.Button] -and $ctrl.Tag -ne "always-on") {
            $ctrl.Enabled = $Enabled
        }
    }
}

# =============================================================================
# BACKGROUND JOB + POLLING TIMER (the reliable part)
# =============================================================================
# A WinForms Timer fires on the SAME UI thread as everything else, on a
# normal schedule, as part of the standard message loop - unlike
# Register-ObjectEvent, this reliably runs, every time, with no surprises.
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500  # check for new output twice a second
$timer.Add_Tick({
    if ($script:currentJob) {
        # Receive-Job WITHOUT -Keep drains only output produced since the
        # last time we called it - exactly what we want for incremental
        # polling without re-printing old lines.
        $newOutput = Receive-Job -Job $script:currentJob -ErrorAction SilentlyContinue
        foreach ($line in $newOutput) {
            if ($null -ne $line) { Append-Log $line.ToString() }
        }
        if ($script:currentJob.State -in @('Completed', 'Failed', 'Stopped')) {
            Append-Log "=============================================================="
            Append-Log "[$($script:currentLabel)] finished (state: $($script:currentJob.State)) at $(Get-Date -Format 'HH:mm:ss')"
            Remove-Job -Job $script:currentJob -Force -ErrorAction SilentlyContinue
            $script:currentJob = $null
            Set-Status "Ready."
            Set-ButtonsEnabled $true
        }
    }
})
$timer.Start()

function Run-Job {
    param([string]$Label, [scriptblock]$ScriptBlock, [object[]]$ArgList)
    if ($script:currentJob) {
        Append-Log ">>> A command is already running - wait for it to finish, or press STOP."
        return
    }
    Set-ButtonsEnabled $false
    Set-Status "Running: $Label ..."
    Append-Log ""
    Append-Log "=============================================================="
    Append-Log "[$Label] $(Get-Date -Format 'HH:mm:ss') - starting"
    Append-Log "=============================================================="
    $script:currentLabel = $Label
    $script:currentJob = Start-Job -ScriptBlock $ScriptBlock -ArgumentList $ArgList
}

function Run-RemoteCommand {
    param([string]$Label, [string]$RemoteCommand)
    Append-Log "  remote command: $RemoteCommand"
    Run-Job -Label $Label -ArgList @($ProjectPath, $RemoteCommand) -ScriptBlock {
        param($ProjectPath, $RemoteCommand)
        Set-Location $ProjectPath
        & vagrant ssh ctrl -c $RemoteCommand 2>&1 | ForEach-Object { $_.ToString() }
    }
}

function Open-InteractiveSession {
    param([string]$NodeName)
    Append-Log "Opening a separate interactive terminal for '$NodeName' ..."
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$ProjectPath'; vagrant ssh $NodeName"
}

# =============================================================================
# BUTTON DEFINITIONS
# =============================================================================
function Add-ActionButton {
    param([string]$Text, [scriptblock]$Action)
    $btn = New-Object System.Windows.Forms.Button
    $btn.Text = $Text
    $btn.AutoSize = $true
    $btn.Padding = New-Object System.Windows.Forms.Padding(6)
    $btn.Add_Click($Action)
    $buttonPanel.Controls.Add($btn)
    return $btn
}

Add-ActionButton "Cluster Status" {
    Run-RemoteCommand -Label "Cluster Status" `
        -RemoteCommand "cd /vagrant/controller && sudo /opt/remediation-venv/bin/python3 cli.py status"
}

$demoBtn1 = Add-ActionButton "DEMO: Single Crash (~40s)" {
    Run-RemoteCommand -Label "DEMO Crash x1" `
        -RemoteCommand "cd /vagrant/controller && sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 1 --scenarios crash --output /tmp/demo_crash.csv"
}
$demoBtn1.BackColor = [System.Drawing.Color]::LightYellow

$demoBtn2 = Add-ActionButton "DEMO: Single Partition (~45s)" {
    Run-RemoteCommand -Label "DEMO Partition x1" `
        -RemoteCommand "cd /vagrant/controller && sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 1 --scenarios partition --output /tmp/demo_partition.csv"
}
$demoBtn2.BackColor = [System.Drawing.Color]::LightYellow

$demoBtn3 = Add-ActionButton "DEMO: Replication Catch-up Proof (~35s)" {
    Run-RemoteCommand -Label "DEMO Replication Proof" `
        -RemoteCommand "bash /vagrant/controller/replication_proof.sh"
}
$demoBtn3.BackColor = [System.Drawing.Color]::LightYellow

$demoBtn4 = Add-ActionButton "DEMO: Backup and Restore Proof (~60s)" {
    Run-RemoteCommand -Label "DEMO Backup/Restore Proof" `
        -RemoteCommand "bash /vagrant/controller/backup_restore_proof.sh"
}
$demoBtn4.BackColor = [System.Drawing.Color]::LightYellow

Add-ActionButton "Vagrant Up (all VMs)" {
    Run-Job -Label "vagrant up" -ArgList @($ProjectPath) -ScriptBlock {
        param($ProjectPath)
        Set-Location $ProjectPath
        & vagrant up 2>&1 | ForEach-Object { $_.ToString() }
    }
}

Add-ActionButton "Run Crash x30" {
    Run-RemoteCommand -Label "Crash x30" `
        -RemoteCommand "cd /vagrant/controller && rm -rf __pycache__ && sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 30 --scenarios crash --output /home/vagrant/gui_crash.csv"
}

Add-ActionButton "Run Partition x30" {
    Run-RemoteCommand -Label "Partition x30" `
        -RemoteCommand "cd /vagrant/controller && rm -rf __pycache__ && sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 30 --scenarios partition --output /home/vagrant/gui_partition.csv"
}

Add-ActionButton "Run Divergence x30" {
    Run-RemoteCommand -Label "Divergence x30" `
        -RemoteCommand "cd /vagrant/controller && rm -rf __pycache__ && sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 30 --scenarios divergence --output /home/vagrant/gui_divergence.csv"
}

Add-ActionButton "Run All 3 Scenarios x30" {
    Run-RemoteCommand -Label "Crash+Partition+Divergence x30" `
        -RemoteCommand "cd /vagrant/controller && rm -rf __pycache__ && sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 30 --scenarios crash,partition,divergence --output /home/vagrant/gui_full.csv"
}

Add-ActionButton "Run Split-Brain x30" {
    Run-RemoteCommand -Label "Split-Brain x30" `
        -RemoteCommand "cd /vagrant/controller && rm -rf __pycache__ && sudo /opt/remediation-venv/bin/python3 split_brain_test.py --runs 30 --output /home/vagrant/gui_split_brain.csv"
}

Add-ActionButton "Run False-Positive Test (60 min)" {
    Run-RemoteCommand -Label "False-Positive Test" `
        -RemoteCommand "cd /vagrant/controller && sudo /opt/remediation-venv/bin/python3 false_positive_test.py --minutes 60 --output /home/vagrant/gui_false_positive.csv"
}

Add-ActionButton "Run Service Availability Test" {
    Run-RemoteCommand -Label "Service Availability" `
        -RemoteCommand "cd /vagrant/controller && rm -rf __pycache__ && sudo /opt/remediation-venv/bin/python3 service_availability_test.py --runs 10 --scenarios crash,partition,divergence --output /home/vagrant/gui_availability.csv"
}

Add-ActionButton "Open Terminal: ctrl" { Open-InteractiveSession "ctrl" }
Add-ActionButton "Open Terminal: ldap1" { Open-InteractiveSession "ldap1" }
Add-ActionButton "Open Terminal: ldap2" { Open-InteractiveSession "ldap2" }
Add-ActionButton "Open Terminal: ldap3" { Open-InteractiveSession "ldap3" }

$stopBtn = Add-ActionButton "STOP Current Command" {
    if ($script:currentJob) {
        Append-Log ">>> Stopping current command ..."
        Stop-Job -Job $script:currentJob -ErrorAction SilentlyContinue
        Remove-Job -Job $script:currentJob -Force -ErrorAction SilentlyContinue
        $script:currentJob = $null
        Set-Status "Ready."
        Set-ButtonsEnabled $true
    } else {
        Append-Log "(nothing is currently running)"
    }
}
$stopBtn.Tag = "always-on"
$stopBtn.BackColor = [System.Drawing.Color]::MistyRose

Add-ActionButton "Clear Log" { $logBox.Clear() }

# =============================================================================
Append-Log "OpenLDAP Remediation Demo - Control Panel ready (v2)."
Append-Log "Project folder: $ProjectPath"
Append-Log "Tip: only one experiment batch should run at a time - wait for one"
Append-Log "     to finish (or press STOP) before starting another."
Append-Log "Note: output updates roughly every half-second, not character by"
Append-Log "      character - a 30-run batch will look quiet for a few seconds"
Append-Log "      between updates, that is normal."
[System.Windows.Forms.Application]::Run($form)
