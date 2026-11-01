import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { strictParams, errorText } from "./helpers.js";
import { existsSync, cpSync, readdirSync, readFileSync, unlinkSync, mkdirSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { projectPlatforms } from "../project-knowledge.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TEMPLATES_DIR = join(__dirname, "..", "..", "..", "templates", "app-knowledge");
const CONFIG_TEMPLATE = join(TEMPLATES_DIR, "config.json");

export function registerAppKnowledgeTools(server: McpServer): void {
  server.registerTool("init_app_knowledge", {
    description: `Initialize or detect an app knowledge base in a project directory.

If the target directory already contains a .quern/knowledge/ folder, returns its current contents summary.
If not, copies the template structure from quern's templates into the target.

After initialization, read the quern://app-knowledge-guide resource for instructions on how to conduct a guided tour of the app with the user.`,
    inputSchema: strictParams({
      project_dir: z.string().describe(
        "Absolute path to the app project's root directory (where the knowledge base should live)"
      ),
    }),
  }, async ({ project_dir }) => {
    try {
      const quernDir = join(project_dir, ".quern");
      const targetDir = join(quernDir, "knowledge");
      const configPath = join(quernDir, "config.json");
      const knowledgeExisted = existsSync(targetDir);

      if (!knowledgeExisted) {
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
        // Remove config.json from knowledge/ — it belongs at .quern/config.json
        const copiedConfig = join(targetDir, "config.json");
        if (existsSync(copiedConfig)) {
          unlinkSync(copiedConfig);
        }
      }

      // Create config.json at .quern/config.json if it doesn't exist
      const configExisted = existsSync(configPath);
      let platformNote = "";
      if (!configExisted && existsSync(CONFIG_TEMPLATE)) {
        mkdirSync(quernDir, { recursive: true });
        cpSync(CONFIG_TEMPLATE, configPath);
        // The platform device requests default to (#582), from the project's
        // files: one platform is written; a cross-platform project is left
        // for the user, since only they know which they test on.
        const found = [...projectPlatforms(project_dir)];
        try {
          const config = JSON.parse(readFileSync(configPath, "utf-8"));
          if (found.length === 1) {
            config.platform = found[0];
            writeFileSync(configPath, JSON.stringify(config, null, 2) + "\n");
            platformNote = `- platform: ${found[0]} (read from the project; device requests default to it)`;
          } else {
            platformNote = found.length === 2
              ? "- platform: not set -- this project builds for iOS and Android. Set \"platform\" in .quern/config.json to the one you test on, or leave it null to choose per request."
              : "- platform: not set -- no Xcode or Android project found. Set \"platform\" in .quern/config.json to \"ios\" or \"android\".";
          }
        } catch {
          platformNote = "- platform: not set (the new config.json could not be read)";
        }
      }

      // Build a summary of what's in the knowledge base
      const summary = summarizeKnowledgeBase(targetDir);

      // Summarize config.json status
      let configStatus: string;
      if (configExisted) {
        const content = readFileSync(configPath, "utf-8");
        try {
          const config = JSON.parse(content);
          const hasContent = config.bundle_id !== null && config.bundle_id !== "";
          configStatus = `- config.json: ${hasContent ? "configured" : "template (needs setup)"}`;
        } catch {
          configStatus = "- config.json: exists (invalid JSON)";
        }
      } else {
        configStatus = "- config.json: created from template (needs setup)";
      }

      const status = knowledgeExisted
        ? "Existing app knowledge base found."
        : "App knowledge base initialized from templates.";

      const nextSteps = knowledgeExisted
        ? "Read the existing files to understand what's documented, then continue the guided tour to fill gaps."
        : [
            "Next steps:",
            "1. Read the quern://app-knowledge-guide resource for the full guided tour process.",
            "2. Fill in .quern/config.json with the app's bundle ID, app name, workspace, and schemes, and check its platform (\"ios\" or \"android\").",
            "3. Fill in app.md with entry points, global navigation, and test accounts.",
            "4. Launch the app and begin documenting screens with the user.",
          ].join("\n");

      return {
        content: [{
          type: "text" as const,
          text: [status, "", summary, configStatus, ...(platformNote ? [platformNote] : []), "",
            nextSteps].join("\n"),
        }],
      };
    } catch (e) {
      return {
        content: [{
          type: "text" as const,
          text: errorText(e),
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

  const countDeepLinks = (baseDir: string): { total: number; configured: boolean } => {
    const jsonPath = join(baseDir, "deep-links", "deep_links.json");
    if (!existsSync(jsonPath)) return { total: 0, configured: false };
    try {
      const data = JSON.parse(readFileSync(jsonPath, "utf-8"));
      const links = [
        ...(data.deep_links ?? []),
        ...(data.coord_info_links ?? []),
        ...(data.special_links ?? []),
      ];
      const hasDomain = data.staging_domain !== null || data.production_domain !== null;
      return { total: links.length, configured: hasDomain || links.length > 0 };
    } catch {
      return { total: 0, configured: false };
    }
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
  const deepLinkCount = countDeepLinks(dir);
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
  sections.push(`- deep-links/: ${deepLinkCount.total} links${deepLinkCount.configured ? "" : " (template, needs setup)"}`);
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
