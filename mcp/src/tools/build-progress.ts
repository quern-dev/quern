/**
 * Progress for a build the caller is waiting on. `build_and_install` answers
 * only when the build ends -- a clean Gradle build of a large app runs many
 * minutes -- so without this a working build and a hung one look the same
 * (#347). Kept free of MCP and HTTP so a test can run it.
 */

export interface RunningBuild {
  project: string;
  task: string;
  elapsed_s: number;
  current: string;
  tasks_run: number;
  /** "checking the variant", "building", "installing on 2 device(s)"; absent
   * from a server older than the field. */
  stage?: string;
}

function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}

/**
 * The line to show for the build of `projectPath`, given what the server
 * says is running. `projectPath` may be a module inside the build root the
 * server names, so a build matches when either path contains the other; one
 * build running and nothing matching is taken to be ours, since the paths
 * can differ by a symlink.
 */
export function progressMessage(
  builds: RunningBuild[],
  projectPath: string,
  waitedS: number
): string {
  const trim = (p: string) => p.replace(/\/+$/, "");
  const mine = trim(projectPath);
  const matching = builds.filter(
    (b) => mine.startsWith(trim(b.project)) || trim(b.project).startsWith(mine)
  );
  const build = matching[0] ?? (builds.length === 1 ? builds[0] : undefined);
  if (!build) {
    // Before Gradle starts: the environment checks and the variant listing,
    // or an Xcode build, which does not report its tasks.
    return `working (${duration(waitedS)})`;
  }
  const took = duration(build.elapsed_s);
  if (build.stage && build.stage !== "building") {
    return `${build.task || "Gradle"}: ${build.stage} (${took})`;
  }
  const now = build.current ? build.current.replace(/^> Task /, "") : "starting Gradle";
  return `${build.task}: ${now} (${took}, ${build.tasks_run} tasks)`;
}
