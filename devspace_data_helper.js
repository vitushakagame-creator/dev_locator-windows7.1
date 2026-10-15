/* DevSpace backup helper. Works locally with Node.js; no server or hosting service needed. */
const fs = require('node:fs');
const path = require('node:path');

function safeName(value) {
  return String(value || 'user').normalize('NFKC').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/g, '').slice(0, 80) || 'user';
}
function readBackup(filePath) {
  const backup = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  if (!backup || backup.type !== 'devspace-account' || !backup.name) {
    throw new Error('Это не резервная копия аккаунта DevSpace.');
  }
  backup.projects = Array.isArray(backup.projects) ? backup.projects : [];
  return backup;
}
function summarize(backup) {
  const stats = backup.stats || {};
  return {
    nickname: backup.name,
    createdAt: backup.createdAt || null,
    downloadedAt: backup.downloadedAt || null,
    computer: backup.importedOnComputer || backup.deviceInfo?.computerName || 'Не указан в файле',
    projects: backup.projects.length,
    likesReceived: Number(stats.likesReceived || backup.projects.reduce((n, p) => n + Number(p.likes || 0), 0)),
    files: backup.projects.reduce((n, p) => n + (Array.isArray(p.files) ? p.files.length : 0), 0)
  };
}
if (require.main === module) {
  try {
    const backupPath = process.argv[2];
    if (!backupPath) {
      console.log('Использование: node devspace_data_helper.js <путь-к-резервной-копии.json>');
      process.exitCode = 2;
    } else {
      const backup = readBackup(path.resolve(backupPath));
      console.log(JSON.stringify(summarize(backup), null, 2));
    }
  } catch (err) {
    console.error('Ошибка:', err.message);
    process.exitCode = 1;
  }
}
module.exports = { safeName, readBackup, summarize };
