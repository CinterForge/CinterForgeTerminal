/*
 * CinterForgeDiscord.js — CinterForge Terminal browser owner
 * PURPOSE: Keeps Discord channel history and draft state independent from PTYs, with user-facing polling.
 * METHODS: showDiscordMessages, loadOlderDiscordMessages, pollDiscordTab, openDiscordTab
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 1063–1069
CinterForgeState.discordMessageText = message => {
    // Discord text is untrusted. Never feed ANSI/control sequences to the VT.
    const content = String(message.content || '').replace(/[\x00-\x08\x0B-\x1F\x7F-\x9F]/g, '');
    const date = message.timestamp && !Number.isNaN(Date.parse(message.timestamp))
      ? new Date(message.timestamp).toLocaleString() + ' · ' : '';
    return `${date}${message.author}: ${content}\n`;
  };

  // Original source lines 1073–1089
CinterForgeState.showDiscordMessages = function showDiscordMessages(tab, messages, older = false) {
    const fresh = [];
    for (const message of messages) {
      if (!message?.id || tab.received.has(message.id)) continue;
      tab.received.add(message.id);
      fresh.push(message);
    }
    if (!fresh.length) return;
    if (older) {
      tab.oldestId = fresh[0].id;
      tab.display.prepend(fresh.map(CinterForgeState.discordMessageText).join(''));
    } else {
      if (!tab.oldestId) tab.oldestId = fresh[0].id;
      tab.newestId = fresh[fresh.length - 1].id;
      tab.display.push(fresh.map(CinterForgeState.discordMessageText).join(''));
    }
  };

  // Original source lines 1094–1111
CinterForgeState.loadOlderDiscordMessages = async function loadOlderDiscordMessages(tab) {
    if (tab.closed || !tab.historyReady || !tab.hasOlder || !tab.oldestId || tab.loadingOlder) return;
    tab.loadingOlder = true;
    tab.followTail = false;
    const generation = tab.pollGeneration;
    try {
      const result = await CinterForgeState.api('/api/discord/messages?channel=' + encodeURIComponent(tab.channelId) +
        '&before=' + encodeURIComponent(tab.oldestId));
      if (tab.closed || tab.pollGeneration !== generation) return;
      CinterForgeState.showDiscordMessages(tab, result.messages || [], true);
      tab.hasOlder = !!result.has_more && (result.messages || []).length > 0;
    } catch (error) {
      if (CinterForgeState.activeTab === tab) CinterForgeState.notice('Discord history: ' + error.message);
    }
    finally {
      tab.loadingOlder = false;
    }
  };

  // Original source lines 1116–1135
CinterForgeState.pollDiscordTab = async function pollDiscordTab(tab) {
    const generation = ++tab.pollGeneration;
    while (!tab.closed && tab.pollGeneration === generation) {
      try {
        const cursor = tab.historyReady && tab.newestId ? '&after=' + encodeURIComponent(tab.newestId) : '';
        const result = await CinterForgeState.api('/api/discord/messages?channel=' + encodeURIComponent(tab.channelId) + cursor);
        if (tab.closed || tab.pollGeneration !== generation) return;
        if (!tab.historyReady) {
          tab.historyReady = true;
          tab.hasOlder = !!result.has_more;
          tab.display.reset();
          if (!(result.messages || []).length) tab.display.push(`No messages yet in #${tab.channel}\n`);
        }
        CinterForgeState.showDiscordMessages(tab, result.messages || []);
      } catch (error) {
        if (CinterForgeState.activeTab === tab) CinterForgeState.notice('Discord history: ' + error.message);
      }
      await new Promise(resolve => setTimeout(resolve, 3000));
    }
  };

  // Original source lines 1141–1177
CinterForgeState.openDiscordTab = async function openDiscordTab(action) {
    const existing = CinterForgeState.tabs.find(tab => !tab.closed && tab.kind === 'discord' && tab.channelId === action.channel_id);
    if (existing) {
      await CinterForgeState.activateTab(existing);
      return;
    }
    const buffer = CinterForgeState.makeBuffer();
    const tab = {
      key: ++CinterForgeState.tabSerial,
      label: '#' + action.channel,
      profileId: 'modern',
      backend: 'discord',
      kind: 'discord',
      guild: action.guild,
      channel: action.channel,
      channelId: action.channel_id,
      bot: action.bot,
      session: null,
      closed: false,
      pollGeneration: 0,
      received: new Set(),
      draft: '',
      oldestId: null,
      newestId: null,
      historyReady: false,
      hasOlder: false,
      loadingOlder: false,
      followTail: true,
      display: new CinterForgeState.VTOutput(buffer),
      buffer
    };
    tab.display.target = tab.buffer;
    CinterForgeState.tabs.push(tab);
    await CinterForgeState.activateTab(tab);
    tab.display.push(`Loading recent history from ${tab.guild} / #${tab.channel}…\n`);
    void CinterForgeState.pollDiscordTab(tab);
  };

  // The server probes credentials at launch and exposes only boolean grants
  // through the existing authenticated /api/status route. Never poll a disabled
  // Discord endpoint: it correctly replies "Unknown command", and repeatedly
  // hitting it floods the HTTP log for every connected Terminal session.
  let discordEnabled = false;
  void CinterForgeState.api('/api/status').then(status => {
    discordEnabled = status?.capabilities?.discord === true;
  }).catch(() => { discordEnabled = false; });

  setInterval(async () => {
    if (!discordEnabled || document.visibilityState === 'hidden') return;
    const ids = CinterForgeState.tabs.filter(tab => tab.session?.id && !tab.closed).map(tab => tab.session.id);
    if (!ids.length) return;
    try {
      const action = await CinterForgeState.api('/api/discord/next', { session_ids: ids });
      if (action.channel_id) await CinterForgeState.openDiscordTab(action);
    } catch (error) {
      // The only supported grant transition is a new app launch. If the
      // server revokes Discord meanwhile, stop further traffic in this page.
      if (/unknown command/i.test(String(error?.message || ''))) discordEnabled = false;
    }
  }, 1100);

})();
