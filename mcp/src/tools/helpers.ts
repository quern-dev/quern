import { z } from "zod";

/**
 * Wraps a raw zod shape with z.object().strict() so unknown keys are rejected.
 * Used with server.registerTool()'s inputSchema property.
 */
export function strictParams<T extends z.ZodRawShape>(shape: T) {
  return z.object(shape).strict();
}
