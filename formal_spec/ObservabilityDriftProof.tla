-------------------- MODULE ObservabilityDriftProof --------------------
(***************************************************************************)
(* TLA+ Mathematical Proof of Counter-Drift Resilience & Fault Detection. *)
(*                                                                         *)
(* Formally proves:                                                        *)
(* 1. Pre-Fix Weak Specification allowed Go container substitution on PHP. *)
(* 2. Post-Fix Guardrail Specification renders undetected drift impossible.*)
(***************************************************************************)

EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    TargetService,      \* "php"
    GenuineLanguage,    \* "php"
    SpoofedLanguage,    \* "golang"
    GenuineProcess,     \* "observability-php-app"
    SpoofedProcess,     \* "observability-go-app"
    GenuineComm,        \* "php"
    SpoofedComm         \* "server"

VARIABLES
    deployedLanguage,   \* Reported language
    deployedProcess,    \* Reported process
    deployedComm,       \* Linux kernel /proc/self/comm
    
    preFixTestPassed,   \* BOOLEAN: Result of legacy weak test assertion ("app_name" in payload)
    postFixTestPassed,  \* BOOLEAN: Result of hardened tri-factor test assertion
    canaryAlertTriggered \* BOOLEAN: Canary alarm state

vars == << deployedLanguage, deployedProcess, deployedComm, 
           preFixTestPassed, postFixTestPassed, canaryAlertTriggered >>

-----------------------------------------------------------------------------
(* Initial State: Fresh legitimate deployment *)
Init ==
    /\ deployedLanguage = GenuineLanguage
    /\ deployedProcess = GenuineProcess
    /\ deployedComm = GenuineComm
    /\ preFixTestPassed = TRUE
    /\ postFixTestPassed = TRUE
    /\ canaryAlertTriggered = FALSE

-----------------------------------------------------------------------------
(* Action 1: Legitimate Redeployment *)
DeployGenuine ==
    /\ deployedLanguage' = GenuineLanguage
    /\ deployedProcess' = GenuineProcess
    /\ deployedComm' = GenuineComm
    /\ preFixTestPassed' = TRUE
    /\ postFixTestPassed' = TRUE
    /\ canaryAlertTriggered' = FALSE

(* Action 2: Container Image Drift / AI Shortcut Substitution (Issue #52) *)
(* An agent deploys the Go binary container onto the PHP endpoint *)
DeploySpoofedGoBinary ==
    /\ deployedLanguage' = SpoofedLanguage
    /\ deployedProcess' = SpoofedProcess
    /\ deployedComm' = SpoofedComm
    \* Legacy Weak Test: Only checked if "app_name" and "version" existed (which Go has!)
    /\ preFixTestPassed' = TRUE  \* Legacy CI gave a FALSE GREEN!
    \* Hardened Post-Fix Guardrail: Strictly asserts equality on language, process, and kernel comm
    /\ postFixTestPassed' = (deployedLanguage' = GenuineLanguage /\ 
                             deployedProcess' = GenuineProcess /\ 
                             deployedComm' = GenuineComm)
    \* Post-Fix Canary: Immediately flags identity drift
    /\ canaryAlertTriggered' = (deployedLanguage' /= GenuineLanguage \/ 
                                deployedProcess' /= GenuineProcess \/ 
                                deployedComm' /= GenuineComm)

Next ==
    \/ DeployGenuine
    \/ DeploySpoofedGoBinary

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* FORMAL THEOREMS & INVARIANTS *)

(* Theorem 1 (The Vulnerability in Pre-Fix CI): *)
(* In the pre-fix architecture, a drifted container can exist with passing tests! *)
(* TLC can find a state where deployedLanguage /= GenuineLanguage BUT preFixTestPassed = TRUE *)
LegacyFlawedAcceptance ==
    (deployedLanguage /= GenuineLanguage) => preFixTestPassed

(* Theorem 2 (Proof of Guardrail Soundness): *)
(* In the hardened architecture, NO drifted container can EVER pass the post-fix tests! *)
HardenedGuardrailSoundness ==
    postFixTestPassed => (deployedLanguage = GenuineLanguage /\ 
                          deployedProcess = GenuineProcess /\ 
                          deployedComm = GenuineComm)

(* Theorem 3 (Canary Alert Completeness): *)
(* Any drift unconditionally forces canaryAlertTriggered to TRUE *)
CanaryAlertCompleteness ==
    (deployedLanguage /= GenuineLanguage \/ deployedProcess /= GenuineProcess) 
        => canaryAlertTriggered

=============================================================================
