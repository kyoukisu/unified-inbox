import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
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
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.tmp-${process.pid}`;
  await writeFile(temporary, `${value}\n`, { encoding: "utf8", mode: 0o600 });
  await chmod(temporary, 0o600);
  await rename(temporary, path);
}
