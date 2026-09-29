#!/usr/bin/env node
import { type RunBinding } from './config.js';
import type { AuxiliaryDecision, AuxiliaryPurpose, LeaseIdentity, LeaseLimits } from './gateway_lease.js';
export interface BrokerMainConfig {
    readonly run: RunBinding;
    readonly trialId: string;
    readonly sessionId: string;
    readonly configDigest: string;
    readonly identity: LeaseIdentity;
    readonly limits: LeaseLimits;
    readonly maxOutputTokens: number;
    readonly timeoutMs?: number;
    readonly tokenTtlMs?: number;
    readonly auxiliaryPolicy?: Readonly<Partial<Record<AuxiliaryPurpose, AuxiliaryDecision>>>;
    readonly listen: {
        readonly host: string;
        readonly port?: number;
        readonly tls?: {
            readonly key: string;
            readonly cert: string;
        };
    };
    readonly tokenOut: string;
    readonly upstream: {
        readonly provider: string;
        readonly baseUrl: string;
        readonly apiKeyEnv: string;
        readonly model: string;
        readonly timeoutMs?: number;
        readonly reasoningEfforts?: readonly string[];
    };
    readonly tokenCount?: {
        readonly endpoint?: string;
        readonly margin?: number;
        readonly timeoutMs?: number;
    };
}
export declare function parseBrokerMainConfig(raw: unknown): BrokerMainConfig;
export declare function main(argv?: readonly string[]): Promise<number>;
