export const STOP_REASONS = Object.freeze([
    'agent_exit_0',
    'agent_exit_nonzero',
    'agent_claimed_done',
    'budget_exhausted',
    'timeout_killed',
    'crashed',
    'infra_error',
]);
export function isStopReason(value) {
    return typeof value === 'string' && STOP_REASONS.includes(value);
}
export function deriveStopReason(observations) {
    const terminal = observations.terminalReason;
    if (terminal !== undefined && !isStopReason(terminal))
        throw new TypeError('Invalid terminal stop reason');
    // Lost enforcement or invalid identity cannot be repaired by nominal completion.
    if (observations.controlLost
        || (observations.refusal !== undefined && observations.refusal.kind !== 'budget_exhausted')) {
        return 'infra_error';
    }
    // Explicit failures outrank success and ordinary budget refusal.
    if (terminal !== undefined && terminal !== 'agent_exit_0' && terminal !== 'agent_claimed_done') {
        return terminal;
    }
    if (observations.refusal)
        return 'budget_exhausted';
    // Neither a completed turn nor disposal proves that persistence succeeded.
    return terminal ?? 'infra_error';
}
