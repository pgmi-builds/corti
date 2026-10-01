import z from "@deepseek-ai/schemastery";
export declare const name = "corti-memory";
export declare const inject: string[];
/**
 * Structural face of a resolved volatile boolean: dsh 0.1.7 hands volatile
 * Config fields over as live refs (`.get()` re-reads after every settings
 * write), while schema defaults and tests see plain booleans.
 */
export type VolatileBoolean = boolean | {
    get(): boolean;
};
export interface Config {
    baseUrl: string;
    appId: string;
    projectId: string;
    userId: string;
    agentId: string;
    recallTopK: number;
    injectTopK: number;
    /** Random entries drawn from the recency window (startup catalog size). */
    recencySample: number;
    /** How many of the newest records the random sample is drawn from. */
    recencyWindow: number;
    maxInjectChars: number;
    autoCapture: boolean;
    /** Master on/off — volatile, edited live from the client half's General row. */
    enabled: VolatileBoolean;
}
/**
 * The entry's Config schema. Only `enabled` is volatile: that is what makes
 * the entry appear in `remote.settings` describe/update (dsh 0.1.7 settings
 * model — the namespace is the Loader entry id, `corti-memory`), with edits
 * landing as profile-layer patch overrides the runtime re-reads live. Every
 * other field stays non-volatile, so settings-form writes never touch them
 * (a patch file still can).
 */
export declare const Config: z<Config>;
export declare function apply(ctx: any, config: Config): Promise<void>;
