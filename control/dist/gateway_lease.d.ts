import { LlmAdapter, LlmError } from '@deepseek-ai/dsh-llm';
import type { GenerateOptions, LlmResolvedModelInfo, StreamChunk, TokenUsage } from '@deepseek-ai/dsh-llm';
import { type EvalControlConfig, type RunBinding } from './config.js';
import { type StopReason } from './stop_reason.js';
export declare const GATEWAY_PROTOCOL = "aeval-model-broker/3";
export declare const MAX_WIRE_BYTES: number;
/** Purposes a runtime may declare for an advisory (non-turn) model call. */
export type AuxiliaryPurpose = 'compaction' | 'session-title';
/** Per-purpose owner decision: refuse before dispatch, or allow and account. */
export type AuxiliaryDecision = 'refuse' | 'allow';
/** The complete, resolved per-purpose policy a lease serves. */
export type AuxiliaryPolicy = Readonly<Record<AuxiliaryPurpose, AuxiliaryDecision>>;
/**
 * Resolve the authored auxiliary policy against the legacy blanket flag.
 *
 * An explicit per-purpose decision always wins; purposes without one take
 * ``refuseAuxiliaryCalls`` (default refuse). Both the broker and the sandbox
 * adapter resolve with this exact function, so ``/info`` comparisons are
 * authoritative rather than field-by-field approximations.
 */
export declare function resolveAuxiliaryPolicy(partial: Readonly<Partial<Record<AuxiliaryPurpose, AuxiliaryDecision>>> | undefined, refuseAll: boolean | undefined): AuxiliaryPolicy;
export declare function isAuxiliaryPurpose(value: unknown): value is AuxiliaryPurpose;
export interface LeaseIdentity {
    readonly provider: string;
    readonly model: string;
    readonly reasoningEffort?: string;
}
export interface LeaseLimits {
    readonly maxSteps?: number;
    readonly maxTokens?: number;
}
export interface BrokerInfo {
    readonly protocol: typeof GATEWAY_PROTOCOL;
    readonly run: RunBinding;
    readonly trialId: string;
    readonly sessionId: string;
    readonly configDigest: string;
    readonly identity: LeaseIdentity;
    readonly limits: LeaseLimits;
    readonly auxiliaryPolicy: AuxiliaryPolicy;
    readonly usedSteps: number;
    readonly usedTokens: number;
    readonly reservedTokens: number;
    readonly stopReason?: StopReason;
    readonly model: LlmResolvedModelInfo;
}
export declare class GatewayError extends LlmError {
    readonly stopReason: StopReason;
    constructor(code: string, stopReason?: StopReason);
}
export declare function abortable<T>(operation: () => T | PromiseLike<T>, signal: AbortSignal): Promise<T>;
export declare function detachedCleanup(operation: () => unknown): void;
export declare function objectOf(value: unknown): Record<string, unknown>;
export declare function tokenCount(value: unknown): number;
export declare function usageTotals(usage: TokenUsage): {
    input: number;
    total: number;
};
export declare function parseStreamChunk(raw: unknown): StreamChunk;
/**
 * The exact key set the broker's trust boundary accepts. Kept next to
 * ``parseBrokerRequest`` so the two cannot drift apart silently.
 */
export declare const BROKER_WIRE_KEYS: readonly ["provider", "model", "reasoningEffort", "messages", "system", "tools", "temperature", "maxTokens", "stop", "sessionId", "purpose"];
/**
 * Project DSH's ``GenerateOptions`` onto the broker wire contract.
 *
 * DSH carries fields the wire does not define — ``toolHistory`` is
 * optional by contract ("omission sends complete declarations without
 * tool updates") and ``signal`` is transport-local — so forwarding the
 * object verbatim makes the broker answer ``AEVAL_INVALID_REQUEST``
 * (found by running a real sandbox against a real broker).
 */
export declare function wireBodyOf(options: GenerateOptions): Record<string, unknown>;
export declare function parseBrokerRequest(raw: unknown): GenerateOptions;
export declare function readJobToken(path: string): string;
/**
 * One gateway rejection, recorded as evidence about consumption.
 *
 * A code in ``PRE_DISPATCH_REJECTION_CODES`` means the broker answered
 * without dispatching, so the request provably consumed zero tokens. The
 * sandbox transport persists these next to the request purpose, and the
 * transcript reducer uses them to keep an advisory call (a session-title
 * request) from being counted as unaccounted model work.
 */
export interface GatewayRejectionRecord {
    readonly code: string;
    readonly purpose?: string;
}
/**
 * One dispatched auxiliary model call, recorded with the usage the broker
 * metered on the wire. The session an auxiliary call belongs to never
 * settles its tokens as an ``assistant/message`` sample, so the sandbox
 * transport persists this beside the descriptor and the transcript reducer
 * merges it into the accounted totals — an allowed compaction call is
 * accounted, never silently dropped.
 */
export interface GatewayDispatchRecord {
    readonly code: 'AEVAL_AUXILIARY_DISPATCHED';
    readonly purpose: string;
    readonly usage: {
        readonly inputTokens: number;
        readonly outputTokens: number;
        readonly cacheReadTokens?: number;
        readonly cacheWriteTokens?: number;
        readonly totalTokens?: number;
        readonly reasoningTokens?: number;
    };
}
export declare class BrokerAdapter extends LlmAdapter {
    #private;
    constructor(config: EvalControlConfig, jobToken: string, onRejection?: (record: GatewayRejectionRecord) => void, onDispatch?: (record: GatewayDispatchRecord) => void);
    observeTerminal(listener: (reason: StopReason) => void): () => void;
    private request;
    info(signal?: AbortSignal): Promise<BrokerInfo>;
    providerRetryPolicy(): import("@deepseek-ai/dsh-llm").ResolvedRetryPolicy;
    resolveModel(provider: string, model: string, signal?: AbortSignal): Promise<LlmResolvedModelInfo>;
    stream(options: GenerateOptions): AsyncIterable<StreamChunk>;
}
