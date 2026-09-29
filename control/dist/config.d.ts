import z from '@deepseek-ai/schemastery';
import type { AuxiliaryDecision, AuxiliaryPurpose } from './gateway_lease.js';
export interface RunBinding {
    readonly run_id: string;
    readonly job_config_hash: string;
    readonly config_file_sha256: string;
    readonly runtime_lock_digest: string;
}
export interface ToolFaceConfig {
    readonly allow?: readonly string[];
    readonly deny?: readonly string[];
}
export interface LineageConfig {
    readonly parentSessionId?: string;
    readonly parentTrialId?: string;
    readonly forkStep?: number;
}
export interface EvalControlConfig {
    readonly run: RunBinding;
    readonly trialId: string;
    readonly sessionId: string;
    readonly sessionRoot: string;
    readonly ownerFinalize?: boolean;
    readonly configDigest: string;
    readonly provider: string;
    readonly model: string;
    readonly reasoningEffort?: string;
    readonly maxSteps?: number;
    readonly maxTokens?: number;
    readonly tools?: ToolFaceConfig;
    readonly lineage?: LineageConfig;
    readonly bundlePath: string;
    readonly gatewayUrl: string;
    readonly jobTokenFile: string;
    readonly refuseAuxiliaryCalls: boolean;
    /**
     * Per-purpose decisions for advisory model calls, as authored (D47).
     * Explicit entries win over ``refuseAuxiliaryCalls``; missing entries take
     * that blanket flag (default refuse). Kept as-authored so the config
     * digest matches the harness-composed config field for field; the resolved
     * complete map is computed at the comparison sites via
     * ``resolveAuxiliaryPolicy``.
     */
    readonly auxiliaryPolicy?: Readonly<Partial<Record<AuxiliaryPurpose, AuxiliaryDecision>>>;
}
export declare const EvalControlConfigFields: Readonly<{
    run: z<Schemastery.ObjectS<NoInfer<{
        run_id: z<string, string, "defined">;
        job_config_hash: z<string, string, "defined">;
        config_file_sha256: z<string, string, "defined">;
        runtime_lock_digest: z<string, string, "defined">;
    }>>, Schemastery.ObjectT<NoInfer<{
        run_id: z<string, string, "defined">;
        job_config_hash: z<string, string, "defined">;
        config_file_sha256: z<string, string, "defined">;
        runtime_lock_digest: z<string, string, "defined">;
    }>>, "defined">;
    trialId: z<string, string, "defined">;
    sessionId: z<string, string, "defined">;
    sessionRoot: z<string, string, "defined">;
    ownerFinalize: z<boolean, boolean, "plain">;
    configDigest: z<string, string, "defined">;
    provider: z<string, string, "defined">;
    model: z<string, string, "defined">;
    reasoningEffort: z<string, string, "plain">;
    maxSteps: z<number, number, "plain">;
    maxTokens: z<number, number, "plain">;
    tools: z<Schemastery.ObjectS<NoInfer<{
        allow: z<string[], string[], "plain">;
        deny: z<string[], string[], "plain">;
    }>>, Schemastery.ObjectT<NoInfer<{
        allow: z<string[], string[], "plain">;
        deny: z<string[], string[], "plain">;
    }>>, "plain">;
    lineage: z<Schemastery.ObjectS<NoInfer<{
        parentSessionId: z<string, string, "plain">;
        parentTrialId: z<string, string, "plain">;
        forkStep: z<number, number, "plain">;
    }>>, Schemastery.ObjectT<NoInfer<{
        parentSessionId: z<string, string, "plain">;
        parentTrialId: z<string, string, "plain">;
        forkStep: z<number, number, "plain">;
    }>>, "plain">;
    bundlePath: z<string, string, "plain">;
    gatewayUrl: z<string, string, "defined">;
    jobTokenFile: z<string, string, "defined">;
    refuseAuxiliaryCalls: z<boolean, boolean, "plain">;
    auxiliaryPolicy: z<Schemastery.ObjectS<NoInfer<{
        compaction: z<string, string, "plain">;
        'session-title': z<string, string, "plain">;
    }>>, Schemastery.ObjectT<NoInfer<{
        compaction: z<string, string, "plain">;
        'session-title': z<string, string, "plain">;
    }>>, "plain">;
}>;
export declare class EvalControlConfigError extends Error {
    readonly name = "EvalControlConfigError";
}
export declare function validateIdentifier(value: unknown, field: string): string;
export declare function validateSha256Digest(value: unknown, field: string): string;
export declare function validateRunBinding(raw: unknown): RunBinding;
export declare function validateSessionRoot(value: string): string;
export declare function resolveEvalControlConfig(raw: unknown): EvalControlConfig;
/** Hash the complete resolved config except configDigest; this does not establish owner trust. */
export declare function digestEvalControlConfig(config: EvalControlConfig): string;
export interface ConfigStandardSchema {
    readonly '~standard': {
        readonly version: 1;
        readonly vendor: string;
        readonly types?: {
            readonly input: unknown;
            readonly output: EvalControlConfig;
        };
        readonly validate: (value: unknown) => {
            readonly value: EvalControlConfig;
            readonly issues?: undefined;
        } | {
            readonly issues: readonly {
                readonly message: string;
            }[];
        };
    };
}
export declare const EvalControlConfigSchema: ConfigStandardSchema;
