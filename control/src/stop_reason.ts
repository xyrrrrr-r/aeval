export const STOP_REASONS = Object.freeze([
  'agent_exit_0',
  'agent_exit_nonzero',
  'agent_claimed_done',
  'budget_exhausted',
  'timeout_killed',
  'crashed',
  'infra_error',
] as const);

export type StopReason = (typeof STOP_REASONS)[number];

export function isStopReason(value: unknown): value is StopReason {
  return typeof value === 'string' && (STOP_REASONS as readonly string[]).includes(value);
}

export type RefusalKind = 'budget_exhausted' | 'identity_mismatch' | 'auxiliary_call';

export interface RunObservations {
  readonly refusal?: { readonly kind: RefusalKind };
  readonly lastTurnEndKind?: string;
  readonly currentTurn?: number;
  readonly lastTurnEndTurn?: number;
  readonly turnInProgress?: boolean;
  readonly sessionDisposed: boolean;
  readonly controlLost?: boolean;
  /** A final observation supplied by the trusted host, never by the agent. */
  readonly terminalReason?: StopReason;
}

export function deriveStopReason(observations: RunObservations): StopReason {
  const terminal = observations.terminalReason;
  if (terminal !== undefined && !isStopReason(terminal)) throw new TypeError('Invalid terminal stop reason');
  // Lost enforcement or invalid identity cannot be repaired by nominal completion.
  if (observations.controlLost
    || (observations.refusal !== undefined && observations.refusal.kind !== 'budget_exhausted')) {
    return 'infra_error';
  }
  // Explicit failures outrank success and ordinary budget refusal.
  if (terminal !== undefined && terminal !== 'agent_exit_0' && terminal !== 'agent_claimed_done') {
    return terminal;
  }
  if (observations.refusal) return 'budget_exhausted';
  // Neither a completed turn nor disposal proves that persistence succeeded.
  return terminal ?? 'infra_error';
}
