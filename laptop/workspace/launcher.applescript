on run
    try
        set projectFolder to choose folder with prompt "Choose the local project to open in CCFleet"
        set optionsList to {"Antigravity", "Claude Code", "Pair Antigravity again"}
        set picked to choose from list optionsList with title "CCFleet Workspace" with prompt "Choose an agent for this project." default items {"Antigravity"}
        if picked is false then return
        set mode to item 1 of picked
        set resourceDir to POSIX path of (path to resource "start.zsh")
        set runner to "none"
        if mode is not "Claude Code" then
            set testChoice to choose from list {"No test command", "pytest", "npm", "go", "cargo"} with title "Local tests" with prompt "Which test command may Antigravity request? Each run requires your approval." default items {"No test command"}
            if testChoice is false then return
            if item 1 of testChoice is not "No test command" then set runner to item 1 of testChoice
        end if
        do shell script "/usr/bin/open -na Ghostty --args -e /bin/zsh " & quoted form of resourceDir & " " & quoted form of mode & " " & quoted form of POSIX path of projectFolder & " " & quoted form of runner
    on error msg number errorNumber
        if errorNumber is not -128 then display alert "CCFleet Workspace" message msg as warning
    end try
end run
