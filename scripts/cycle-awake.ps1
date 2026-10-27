# A thread-scoped Windows idle-sleep request; never changes the power plan.
# API: https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
function Start-CycleAwake {
    if (-not ('AgentDispatcher.CyclePower' -as [type])) {
        Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
namespace AgentDispatcher {
    public static class CyclePower {
        [DllImport("kernel32.dll")]
        public static extern uint SetThreadExecutionState(uint flags);
    }
}
'@
    }
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED; no display or away-mode flag requested.
    return [AgentDispatcher.CyclePower]::SetThreadExecutionState([uint32]2147483649)
}

function Stop-CycleAwake {
    param([uint32]$PreviousState)
    # Restore the caller's prior state, retaining continuous flag semantics.
    return [AgentDispatcher.CyclePower]::SetThreadExecutionState(
        ($PreviousState -bor [uint32]2147483648)
    )
}
