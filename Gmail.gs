// Вставить в Код.gs проекта владельца Gmail.
// Настройки проекта -> Свойства скрипта:
// INGEST_URL = https://ваш-домен.bothost.tech/ingest
// INGEST_SECRET = секрет из Bothost

const SUBJECT_WORDS = ['Рынок бытовой химии', 'НОВОСТИ'];
const PROPERTY = PropertiesService.getScriptProperties();

function conf_(name) {
  const value = PROPERTY.getProperty(name);
  if (!value) throw new Error('Не задано свойство: ' + name);
  return value;
}

function subjectMatches_(subject) {
  const s = String(subject || '').toLowerCase();
  return SUBJECT_WORDS.some(word => s.indexOf(word.toLowerCase()) !== -1);
}

function getAddresses_() {
  const main = Session.getEffectiveUser().getEmail().toLowerCase();
  if (!main) {
    throw new Error('Не определён Google-аккаунт. Запустить должен владелец Gmail.');
  }
  return [main].concat(GmailApp.getAliases().map(x => x.toLowerCase()));
}

function reallySentByOwner_(message, addresses) {
  const raw = String(message.getFrom() || '').toLowerCase().trim();
  const left = raw.lastIndexOf('<');
  const right = raw.lastIndexOf('>');
  const sender = left >= 0 && right > left
    ? raw.slice(left + 1, right).trim() : raw;
  return addresses.indexOf(sender) >= 0;
}

function getEmails_() {
  const addresses = getAddresses_();
  const seen = {};
  const matches = [];
  for (const word of SUBJECT_WORDS) {
    const query = 'in:sent newer_than:45d subject:"' + word + '"';
    const threads = GmailApp.search(query, 0, 100);
    for (const thread of threads) {
      for (const message of thread.getMessages()) {
        const id = message.getId();
        if (seen[id] || !subjectMatches_(message.getSubject()) ||
            !reallySentByOwner_(message, addresses)) {
          continue;
        }
        seen[id] = true;
        matches.push(message);
      }
    }
  }
  return matches.sort((a, b) => a.getDate().getTime() - b.getDate().getTime());
}

function getLinks_(message, plain) {
  const html = String(message.getBody() || '');
  const found = [];
  const seen = {};
  let cursor = 0;
  while (cursor < html.length && found.length < 25) {
    let pos = html.indexOf('href="', cursor);
    let quote = '"';
    const other = html.indexOf("href='", cursor);
    if (pos < 0 || (other >= 0 && other < pos)) {
      pos = other;
      quote = "'";
    }
    if (pos < 0) break;
    const from = pos + 6;
    const to = html.indexOf(quote, from);
    if (to < 0) break;
    cursor = to + 1;
    const link = html.slice(from, to).split('&amp;').join('&');
    if (!link.startsWith('https://') && !link.startsWith('http://')) continue;
    if (plain.indexOf(link) >= 0 || seen[link]) continue;
    seen[link] = true;
    found.push(link);
  }
  return found;
}

function sendToBot_(message, test) {
  const body = String(message.getPlainBody() || '').trim();
  const payload = {
    source_id: 'gmail-' + message.getId(),
    subject: message.getSubject(),
    body: body,
    source_date: Utilities.formatDate(
      message.getDate(), 'Europe/Moscow', 'dd.MM.yyyy HH:mm'
    ),
    links: getLinks_(message, body),
    test: Boolean(test)
  };

  const result = UrlFetchApp.fetch(conf_('INGEST_URL'), {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Ingest-Secret': conf_('INGEST_SECRET') },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  const code = result.getResponseCode();
  if (code !== 200) {
    throw new Error('Ошибка пересылки HTTP ' + code + ': ' +
      result.getContentText().slice(0, 500));
  }
  Logger.log('Отправлено: ' + message.getSubject() + ' / ' +
    result.getContentText());
}

// Проверка: одно последнее исходящее письмо, даже если старое.
function testLatestEmail() {
  const messages = getEmails_();
  if (!messages.length) {
    throw new Error('Нет подходящих исходящих писем за последние 45 дней.');
  }
  sendToBot_(messages[messages.length - 1], true);
}

// Фоновая синхронизация новых писем.
function checkMail() {
  const start = Number(conf_('MAIL_START_AT'));
  for (const message of getEmails_()) {
    if (message.getDate().getTime() < start) continue;
    sendToBot_(message, false);
  }
}

// Владелец Gmail запускает один раз.
function activateMailSync() {
  const oldNames = ['pollTelegram', 'forwardNews', 'checkMail'];
  for (const trigger of ScriptApp.getProjectTriggers()) {
    if (oldNames.indexOf(trigger.getHandlerFunction()) !== -1) {
      ScriptApp.deleteTrigger(trigger);
    }
  }
  if (!PROPERTY.getProperty('MAIL_START_AT')) {
    PROPERTY.setProperty('MAIL_START_AT', String(Date.now()));
  }
  ScriptApp.newTrigger('checkMail').timeBased().everyMinutes(5).create();
  Logger.log('Проверка Gmail включена. Интервал: 5 минут.');
}

function stopMailSync() {
  for (const trigger of ScriptApp.getProjectTriggers()) {
    if (trigger.getHandlerFunction() === 'checkMail') {
      ScriptApp.deleteTrigger(trigger);
    }
  }
  Logger.log('Синхронизация Gmail отключена.');
}
