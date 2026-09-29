export declare const STOP_REASONS: readonly ["agent_exit_0", "agent_exit_nonzero", "agent_claimed_done", "budget_exhausted", "timeout_killed", "crashed", "infra_error"];
export type StopReason = (typeof STOP_REASONS)[number];
export declare function isStopReason(value: unknown): value is StopReason;
export type RefusalKind = 'budget_exhausted' | 'identity_mismatch' | 'auxiliary_call';
export interface RunObservations {
    readonly refusal?: {
        readonly kind: RefusalKind;
    };
    readonly lastTurnEndKind?: string;
    readonly currentTurn?: number;
    readonly lastTurnEndTurn?: number;
    readonly turnInProgress?: boolean;
    readonly sessionDisposed: boolean;
    readonly controlLost?: boolean;
    /** A final observation supplied by the trusted host, never by the agent. */
    readonly terminalReason?: StopReason;
}
export declare function deriveStopReason(observations: RunObservations): StopReason;
