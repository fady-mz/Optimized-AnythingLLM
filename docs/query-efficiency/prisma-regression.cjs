// Run only against a newly migrated, disposable database; nonempty data is refused.
const assert = require("node:assert/strict");
const path = require("node:path");
const { readFileSync } = require("node:fs");

const [clientPath, databasePath] = process.argv.slice(2);
assert(clientPath && databasePath, "Usage: node prisma-regression.cjs <generated-client-directory> <disposable-db-file>");
const { PrismaClient } = require(path.resolve(clientPath));
const options = { datasources: { db: { url: `file:${path.resolve(databasePath).replaceAll("\\", "/")}` } } };
const prisma = new PrismaClient(options);
const cutoff = new Date("2026-09-08T00:00:00.000Z");
const indexName = "workspace_chats_user_history_quota_idx";
const migration = readFileSync(path.join(__dirname, "../../server/prisma/migrations/20260909000000_chat_history_quota_index/migration.sql"), "utf8");

function fixtureRows() {
  const rows = [];
  for (const user_id of [null, 1, 2])
    for (const workspaceId of [1, 2])
      for (const thread_id of [null, 7])
        for (const api_session_id of [null, "synthetic-session"])
          for (const include of [true, false])
            for (const offset of [-1, 0, 1])
              rows.push({ user_id, workspaceId, thread_id, api_session_id, include,
                prompt: "synthetic", response: "{}", createdAt: new Date(+cutoff + offset), lastUpdatedAt: cutoff });
  for (let i = 0; i < 35; i++)
    rows.push({ user_id: 1, workspaceId: 1, thread_id: null, api_session_id: null,
      include: true, prompt: `history-${i}`, response: "{}", createdAt: cutoff, lastUpdatedAt: cutoff });
  return rows;
}

async function checkReads(rows) {
  let checks = 0;
  for (const user_id of [null, 1, 2, 999]) {
    const expected = rows.filter(row => row.user_id === user_id && row.createdAt >= cutoff).length;
    assert.equal(await prisma.workspace_chats.count({ where: { user_id, createdAt: { gte: cutoff } } }), expected);
    checks++;
  }
  for (const user_id of [null, 1, 2])
    for (const workspaceId of [1, 2])
      for (const thread_id of [null, 7])
        for (const api_session_id of [null, "synthetic-session"]) {
          const where = { user_id, workspaceId, thread_id, api_session_id, include: true };
          const expected = rows.filter(row => Object.entries(where).every(([key, value]) => row[key] === value))
            .sort((a, b) => b.id - a.id).slice(0, 20);
          assert.deepEqual(await prisma.workspace_chats.findMany({ where, take: 20, orderBy: { id: "desc" } }), expected);
          checks++;
        }
  return checks;
}

async function main() {
  assert.equal(await prisma.users.count(), 0, "Refusing a database containing users");
  assert.equal(await prisma.workspace_chats.count(), 0, "Refusing a database containing chats");
  for (const id of [1, 2]) await prisma.users.create({ data: { id, password: "synthetic-not-a-real-password" } });
  const rows = await prisma.$transaction(fixtureRows().map(data => prisma.workspace_chats.create({ data })));
  let checks = await checkReads(rows);
  const index = await prisma.$queryRawUnsafe(`PRAGMA index_info('${indexName}')`);
  assert.deepEqual(index.map(column => column.name), ["user_id", "workspaceId", "thread_id", "api_session_id", "include", "id", "createdAt"]);
  await prisma.$executeRawUnsafe(`DROP INDEX "${indexName}"`);
  checks += await checkReads(rows);
  await prisma.$executeRawUnsafe(migration);
  checks += await checkReads(rows);

  const created = await prisma.workspace_chats.create({ data: {
    user_id: 1, workspaceId: 1, thread_id: null, api_session_id: null, include: true,
    prompt: "new synthetic turn", response: "{}", createdAt: cutoff, lastUpdatedAt: cutoff,
  } });
  rows.push(created);
  checks += await checkReads(rows);
  rows[rows.length - 1] = await prisma.workspace_chats.update({ where: { id: created.id }, data: { createdAt: new Date(+cutoff - 1), include: false } });
  checks += await checkReads(rows);
  await prisma.workspace_chats.delete({ where: { id: created.id } });
  rows.pop();
  checks += await checkReads(rows);
  assert.deepEqual(await prisma.workspace_chats.findMany({ orderBy: { id: "asc" } }), rows);
  assert.deepEqual(await prisma.$queryRawUnsafe("PRAGMA foreign_key_check"), []);
  assert.equal(Object.values((await prisma.$queryRawUnsafe("PRAGMA integrity_check"))[0])[0], "ok");
  const sqlite = await prisma.$queryRawUnsafe("SELECT sqlite_version() AS version");
  await prisma.$disconnect();
  const reopened = new PrismaClient(options);
  try { assert.deepEqual(await reopened.workspace_chats.findMany({ orderBy: { id: "asc" } }), rows); }
  finally { await reopened.$disconnect(); }
  console.log(JSON.stringify({ passedReadComparisons: checks, fixtureRows: rows.length,
    sqlite, indexColumnsVerified: true, migrationRollbackReapplyPassed: true,
    insertUpdateDeletePassed: true, fullRowsAndReopenPassed: true, integrityPassed: true }, null, 2));
}

main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => prisma.$disconnect());
