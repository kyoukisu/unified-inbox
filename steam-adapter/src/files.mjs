import { mkdir, open, readFile, rename } from "node:fs/promises";
import { dirname } from "node:path";

export async function readRequiredFile(path, label) {
  let value;
  try {
    value = (await readFile(path, "utf8")).trim();
  } catch (error) {
    throw new Error(`Unable to read ${label} file`, { cause: error });
  }
  if (!value) {
    throw new Error(`${label} file is empty`);
  }
  return value;
}

export async function atomicWritePrivate(path, value) {
  const directoryPath = dirname(path);
  await mkdir(directoryPath, { recursive: true, mode: 0o700 });
  const temporary = `${path}.tmp-${process.pid}`;
  const file = await open(temporary, "w", 0o600);
  try {
    await file.writeFile(`${value}\n`, "utf8");
    await file.chmod(0o600);
    await file.sync();
  } finally {
    await file.close();
  }
  await rename(temporary, path);
  const directory = await open(directoryPath, "r");
  try {
    await directory.sync();
  } finally {
    await directory.close();
  }
}
