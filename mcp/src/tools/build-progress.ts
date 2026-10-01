/**
 * Progress for a build the caller is waiting on. `build_and_install` answers
 * only when the build ends -- a clean Gradle build of a large app runs many
 * minutes -- so without this a working build and a hung one look the same
 * (#347). Kept free of MCP and HTTP so a test can run it: the poll is passed
 * in.
 */

export interface RunningBuild {
  project: string;
  task: string;
  elapsed_s: number;
  current: string;
  tasks_run: number;
  /** "checking the variant", "building", "installing on 2 device(s)". */
  stage?: string;
  /** The id this call sent with its request; absent from an older server. */
  progress_id?: string;
}

function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}

/**
 * The line to show for this call's build. Matched by the id the call sent,
 * never by path: two builds can run at once, and an iOS build or an Android
 * one still in its environment checks would otherwise show someone else's
 * Gradle build as its own.
 */
export function progressMessage(
  builds: RunningBuild[],
  progressId: string,
  waitedS: number
): string {
  const build = builds.find((b) => b.progress_id === progressId);
  if (!build) {
    // Before Gradle starts, an Xcode build (which does not report its tasks),
    // or a server too old to say.
    return `working (${duration(waitedS)})`;
  }
  const took = duration(build.elapsed_s);
  if (build.stage && build.stage !== "building") {
    return `${build.task || "Gradle"}: ${build.stage} (${took})`;
  }
  const now = build.current ? build.current.replace(/^> Task /, "") : "starting Gradle";
  return `${build.task}: ${now} (${took}, ${build.tasks_run} tasks)`;
}

export interface ProgressNotification {
  method: "notifications/progress";
  params: { progressToken: string | number; progress: number; message?: string };
}

/** What a tool handler is handed beyond its arguments, as much as is used. */
export interface HandlerExtra {
  _meta?: { progressToken?: string | number };
  signal?: AbortSignal;
  sendNotification: (notification: ProgressNotification) => Promise<void>;
}

/**
 * Send progress notifications every `everyMs` while a build runs, if the
 * caller asked with a progress token; returns the function that stops them.
 *
 * Stopping is final: a tick already waiting on `poll` when the tool answers
 * sends nothing, since a notification after the response is for a token the
 * client has forgotten. A cancelled request stops them too. Best effort
 * throughout -- a poll that fails says nothing about the build.
 */
export function reportProgress(
  extra: HandlerExtra,
  progressId: string,
  poll: () => Promise<RunningBuild[]>,
  everyMs = 10_000
): () => void {
  const token = extra._meta?.progressToken;
  if (token === undefined) return () => {};
  const started = Date.now();
  let stopped = false;
  let busy = false;
  const stop = () => {
    stopped = true;
    clearInterval(timer);
    extra.signal?.removeEventListener("abort", stop);
  };
  const timer = setInterval(async () => {
    if (busy || stopped) return;
    busy = true;
    try {
      const waited = (Date.now() - started) / 1000;
      let builds: RunningBuild[] = [];
      try {
        builds = await poll();
      } catch {
        // An older server has no such route: progress is then just the wait.
      }
      if (stopped) return;
      // progress must only increase: seconds waited does.
      await extra.sendNotification({
        method: "notifications/progress",
        params: { progressToken: token, progress: Math.round(waited),
                  message: progressMessage(builds, progressId, waited) },
      });
    } catch {
      // The client went away; the build call itself will say so.
    } finally {
      busy = false;
    }
  }, everyMs);
  extra.signal?.addEventListener("abort", stop);
  return stop;
}
