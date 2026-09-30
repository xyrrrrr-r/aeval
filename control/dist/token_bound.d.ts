import type { GenerateOptions } from '@deepseek-ai/dsh-llm';
import { type UpstreamProtocol } from './upstream.js';
/**
 * Trusted input-token metering for hard token budgets.
 *
 * A hard `limits.maxTokens` budget is only as trustworthy as its input bound:
 * the broker must refuse the request when the provider-side count of the exact
 * dispatch cannot be obtained. The official dsh-token-meter
 * `CHARS_PER_TOKEN=4` heuristic is NOT a hard upper bound and must never be
 * wired in here — heuristic estimation cannot back a hard budget, so this
 * module provides no heuristic path at all. Without a trusted counting source
 * configured, `startHostBroker` keeps refusing hard budgets outright.
 */
export interface ProviderCountBoundOptions {
    readonly baseUrl: string;
    readonly apiKeyEnv: string;
    /**
     * Wire protocol of the dispatch being counted, so the bound counts the
     * exact body the adapter would send (the two protocols serialize
     * differently); defaults to chat_completions.
     */
    readonly protocol?: UpstreamProtocol;
    /** Absolute URL of the counting endpoint; defaults to `${baseUrl}/tokens/count`. */
    readonly endpoint?: string;
    /** Tokens added to the counted value to absorb framing drift; defaults to 8. */
    readonly margin?: number;
    readonly timeoutMs?: number;
}
/**
 * Bound one request's provider input tokens. Returns the counted value plus
 * the margin; any failure (transport, timeout, non-2xx, malformed payload)
 * throws so the broker refuses the hard-budget request instead of dispatching
 * on an unmeasured prompt.
 */
export type ProviderInputTokenBound = (request: Readonly<GenerateOptions>) => Promise<number>;
/** Build a provider-backed input-token bound for one upstream route. */
export declare function createProviderCountBound(options: ProviderCountBoundOptions): ProviderInputTokenBound;
