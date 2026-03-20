import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { strictParams } from "./helpers.js";
import { existsSync, cpSync, readdirSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TEMPLATES_DIR = join(__dirname, "..", "..", "..", "templates", "app-knowledge");

export function registerAppKnowledgeTools(server: McpServer): void {
  server.registerTool("init_app_knowledge", {
    description: `Initialize or detect an app knowledge base in a project directory.

If the target directory already contains an app-knowledge/ folder, returns its current contents summary.
If not, copies the template structure from quern's templates into the target.

After initialization, read the quern://app-knowledge-guide resource for instructions on how to conduct a guided tour of the app with the user.`,
    inputSchema: strictParams({
      project_dir: z.string().describe(
        "Absolute path to the app project's root directory (where the knowledge base should live)"
      ),
    }),
  }, async ({ project_dir }) => {
    try {
      const targetDir = join(project_dir, "app-knowledge");
      const existed = existsSync(targetDir);

      if (!existed) {
        if (!existsSync(TEMPLATES_DIR)) {
          return {
            content: [{
              type: "text" as const,
              text: `Error: Template directory not found at ${TEMPLATES_DIR}. Is quern installed correctly?`,
            }],
            isError: true,
          };
        }
        cpSync(TEMPLATES_DIR, targetDir, { recursive: true });
      }

      // Build a summary of what's in the knowledge base
      const summary = summarizeKnowledgeBase(targetDir);

      const status = existed
        ? "Existing app knowledge base found."
        : "App knowledge base initialized from templates.";

      const nextSteps = existed
        ? "Read the existing files to understand what's documented, then continue the guided tour to fill gaps."
        : [
            "Next steps:",
            "1. Read the quern://app-knowledge-guide resource for the full guided tour process.",
            "2. Fill in app.md with the app's bundle ID, URL scheme, and other basics.",
            "3. Launch the app and begin documenting screens with the user.",
          ].join("\n");

      return {
        content: [{
          type: "text" as const,
          text: [status, "", summary, "", nextSteps].join("\n"),
        }],
      };
    } catch (e) {
      return {
        content: [{
          type: "text" as const,
          text: `Error: ${e instanceof Error ? e.message : String(e)}`,
        }],
        isError: true,
      };
    }
  });
}

function summarizeKnowledgeBase(dir: string): string {
  const sections: string[] = ["Contents:"];

  const countFiles = (subdir: string, ext = ".md"): number => {
    const fullPath = join(dir, subdir);
    if (!existsSync(fullPath)) return 0;
    return readdirSync(fullPath).filter(
      (f) => f.endsWith(ext) && !f.startsWith("_")
    ).length;
  };

  const countScreensByStatus = (subdir: string): { documented: string[]; stubs: string[] } => {
    const fullPath = join(dir, subdir);
    if (!existsSync(fullPath)) return { documented: [], stubs: [] };
    const documented: string[] = [];
    const stubs: string[] = [];
    for (const f of readdirSync(fullPath)) {
      if (!f.endsWith(".md") || f.startsWith("_")) continue;
      const content = readFileSync(join(fullPath, f), "utf-8");
      const name = f.replace(/\.md$/, "");
      if (content.includes("status: stub")) {
        stubs.push(name);
      } else {
        documented.push(name);
      }
    }
    return { documented, stubs };
  };

  // Check app.md
  const appMd = join(dir, "app.md");
  if (existsSync(appMd)) {
    const content = readFileSync(appMd, "utf-8");
    const hasContent = content.includes('app_name: "') && !content.includes('app_name: ""');
    sections.push(`- app.md: ${hasContent ? "configured" : "template (needs setup)"}`);
  }

  // Check states.md and environments.md
  for (const file of ["states.md", "environments.md"]) {
    const filePath = join(dir, file);
    if (existsSync(filePath)) {
      const content = readFileSync(filePath, "utf-8");
      const hasContent = !content.includes("<!-- ") || content.replace(/<!--[\s\S]*?-->/g, "").trim().split("\n").length > 10;
      sections.push(`- ${file}: ${hasContent ? "configured" : "template (needs setup)"}`);
    }
  }

  const screens = countScreensByStatus("screens");
  const flowCount = countFiles("flows");
  const deepLinkCount = countFiles("deep-links");
  const alertCount = countFiles("alerts");
  const quirkCount = countFiles("quirks");

  const screenParts = [`${screens.documented.length} documented`];
  if (screens.stubs.length > 0) {
    screenParts.push(`${screens.stubs.length} stubs`);
  }
  sections.push(`- screens/: ${screenParts.join(", ")}`);
  if (screens.stubs.length > 0) {
    sections.push(`  Stubs needing visit: ${screens.stubs.join(", ")}`);
  }
  sections.push(`- flows/: ${flowCount} documented`);
  sections.push(`- deep-links/: ${deepLinkCount} documented`);
  sections.push(`- alerts/: ${alertCount} documented`);
  sections.push(`- quirks/: ${quirkCount} documented`);

  // Find screens referenced in leads_to edges but with no file
  const missingScreens = findMissingScreens(dir, new Set([
    ...screens.documented,
    ...screens.stubs,
  ]));
  if (missingScreens.length > 0) {
    sections.push("");
    sections.push(`Undocumented screens (referenced in leads_to but no file):`);
    for (const { name, referencedBy } of missingScreens) {
      sections.push(`  - ${name} (referenced by: ${referencedBy.join(", ")})`);
    }
  }

  return sections.join("\n");
}

function findMissingScreens(
  dir: string,
  existingScreens: Set<string>,
): Array<{ name: string; referencedBy: string[] }> {
  const screensDir = join(dir, "screens");
  if (!existsSync(screensDir)) return [];

  // Also check app.md for leads_to references
  const filesToScan: Array<{ path: string; name: string }> = [];

  for (const f of readdirSync(screensDir)) {
    if (!f.endsWith(".md") || f.startsWith("_")) continue;
    filesToScan.push({ path: join(screensDir, f), name: f.replace(/\.md$/, "") });
  }

  const appMd = join(dir, "app.md");
  if (existsSync(appMd)) {
    filesToScan.push({ path: appMd, name: "app" });
  }

  // Match [[screens/name]] patterns in leads_to sections
  const references = new Map<string, string[]>();
  const screenLinkPattern = /\[\[screens\/([^\]]+)\]\]/g;

  for (const { path, name } of filesToScan) {
    const content = readFileSync(path, "utf-8");
    for (const match of content.matchAll(screenLinkPattern)) {
      const target = match[1];
      if (!existingScreens.has(target)) {
        if (!references.has(target)) references.set(target, []);
        references.get(target)!.push(name);
      }
    }
  }

  return Array.from(references.entries())
    .map(([name, referencedBy]) => ({ name, referencedBy }))
    .sort((a, b) => b.referencedBy.length - a.referencedBy.length);
}
