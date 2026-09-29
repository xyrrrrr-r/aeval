import type { GenerateOptions, LlmAdapter, LlmResolvedModelInfo, StreamChunk } from '@deepseek-ai/dsh-llm';
import type { AuxiliaryDecision, AuxiliaryPurpose, BrokerInfo, LeaseIdentity, LeaseLimits } from './gateway_lease.js';
import { type StopReason } from './stop_reason.js';
import { type RunBinding } from './config.js';
export interface HostBrokerOptions {
    readonly run: RunBinding;
    readonly trialId: string;
    readonly sessionId: string;
    readonly configDigest: string;
    readonly identity: LeaseIdentity;
    readonly limits: LeaseLimits;
    readonly maxOutputTokens: number;
    readonly upstream: LlmAdapter;
    readonly inputTokenUpperBound?: (request: Readonly<GenerateOptions>) => number | Promise<number>;
    /** Legacy blanket decision for purposes without an explicit entry. */
    readonly refuseAuxiliaryCalls?: boolean;
    /**
     * Per-purpose owner decisions for advisory model calls (D47). Explicit
     * entries win; missing entries take ``refuseAuxiliaryCalls`` (default
     * refuse). Allowing a purpose dispatches and meters it — the accounting
     * evidence is the dispatch ledger, not the session.
     */
    readonly auxiliaryPolicy?: Readonly<Partial<Record<AuxiliaryPurpose, AuxiliaryDecision>>>;
    readonly signal: AbortSignal;
    readonly timeoutMs?: number;
    /**
     * Wall-clock lifetime of the served job token, counted from broker startup.
     * Authenticated requests after expiry fail with 401 `AEVAL_TOKEN_EXPIRED`;
     * the process-level auto-stop timer that enforces shutdown lives with the
     * broker host entry, which also owns the token file's cleanup.
     */
    readonly tokenTtlMs?: number;
    readonly listen?: {
        readonly host: string;
        readonly port?: number;
        readonly tls?: {
            readonly key: string;
            readonly cert: string;
        };
    };
    readonly onStop?: (reason: StopReason) => void;
}
export declare function isLoopbackHost(host: string): boolean;
export declare class GatewayLease {
    #private;
    get signal(): AbortSignal;
    constructor(options: HostBrokerOptions, model: LlmResolvedModelInfo);
    stop(reason: StopReason, cause?: string): void;
    snapshot(): BrokerInfo;
    private refuse;
    stream(raw: unknown, callerSignal: AbortSignal): AsyncIterable<StreamChunk>;
}
export interface HostBroker {
    readonly url: string;
    readonly token: string;
    readonly lease: GatewayLease;
    close(reason?: StopReason): Promise<void>;
}
export declare function startHostBroker(options: HostBrokerOptions): Promise<HostBroker>;
export declare function writeJobToken(path: string, token: string): void;
/**
 * Remove a job-token file this host wrote. Only an existing, owned, 0600
 * regular file that is not a symlink is deleted — anything else at the path
 * refuses removal — and every failure surfaces to the caller, because this
 * runs on the credentialed host side where silent cleanup gaps accumulate.
 */
export declare function cleanupJobToken(path: string): void;
