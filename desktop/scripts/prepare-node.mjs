// Copies the running Node into the sidecar runtime before an installer build.
// The unified client must not ask the owner to install Node separately.
import { copyFileSync, existsSync, mkdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const runtimeDir = join(here, "..", "sidecar", "runtime");
const target = join(runtimeDir, "node.exe");
if (process.platform !== "win32") {
  console.error("prepare-node: run on Windows for the Windows installer");
  process.exit(1);
}
if (!existsSync(process.execPath)) {
  console.error("prepare-node: cannot find the running node.exe");
  process.exit(1);
}
mkdirSync(runtimeDir, { recursive: true });
copyFileSync(process.execPath, target);
console.log(`prepare-node: ${process.execPath} -> ${target} (${Math.round(statSync(target).size / 1024 / 1024)} MiB)`);
