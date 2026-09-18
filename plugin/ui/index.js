function chatPlugin() {
    return {
        conversations: [],
        activeId: null,
        activeTitle: '',
        messages: [],
        draft: '',
        loading: false,
        loadingConversations: false,
        editingIndex: null,
        searchQuery: '',
        showArchived: false,
        searchTimer: null,
        mobileThreadSheetOpen: false,
        editingThreadId: null,
        editTitleDraft: '',
        toolActivity: [],
        streamStarted: false,
        streamError: null,
        summary: null,
        summarizing: false,

        async init() {
            await this.loadConversations();
            if (AM.voice) {
                AM.voice.onTranscribe('chat', (text, blob) => {
                    this.draft = this.draft ? this.draft + ' ' + text : text;
                    this.$nextTick(() => { const el = this.$refs.chatInput; if (el) el.focus(); });
                });
            }
            AM.onCleanup(() => {
                if (AM.voice && AM.voice.isRecording()) AM.voice.stopRecording();
                if (AM.tts) AM.tts.stop();
            });
        },

        async loadConversations() {
            this.loadingConversations = true;
            try {
                let url = '/plugins/chat/conversations';
                if (this.showArchived) url += '?archived=true';
                const resp = await AM.fetch(url);
                if (!resp || !resp.ok) {
                    AM.toast('Failed to load conversations', 'error');
                    return;
                }
                this.conversations = await resp.json();
                if (this.conversations.length > 0) {
                    await this.loadConversation(this.conversations[0].id);
                }
            } catch (e) {
                console.error('Chat loadConversations', e);
                AM.toast('Failed to load conversations', 'error');
            } finally {
                this.loadingConversations = false;
            }
        },

        async loadConversation(id) {
            this.activeId = id;
            this.messages = [];
            this.editingIndex = null;
            this.summary = null;
            this.streamError = null;
            this.toolActivity = [];
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + id);
                if (!resp || !resp.ok) {
                    AM.toast('Failed to load conversation', 'error');
                    return;
                }
                const detail = await resp.json();
                this.activeTitle = detail.title || '';
                this.messages = detail.messages.map(m => ({ role: m.role, content: m.content, _editText: '', created_at: m.created_at }));
            } catch (e) {
                console.error('Chat loadConversation', e);
                AM.toast('Failed to load conversation', 'error');
            }
            this.scrollChatBottom();
        },

        startMessageEdit(index) {
            const msg = this.messages[index];
            if (msg) msg._editText = msg.content;
            this.editingIndex = index;
        },

        async newConversation() {
            try {
                const resp = await AM.fetch('/plugins/chat/conversations', {
                    method: 'POST',
                    body: { title: 'New chat' },
                });
                if (!resp || !resp.ok) {
                    AM.toast('Failed to create conversation', 'error');
                    return;
                }
                const conv = await resp.json();
                this.conversations.unshift(conv);
                this.activeId = conv.id;
                this.activeTitle = conv.title || '';
                this.messages = [];
                this.editingIndex = null;
                this.summary = null;
                this.$nextTick(() => { const el = this.$refs.chatInput; if (el) el.focus(); });
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async sendChatMessage() {
            const text = this.draft.trim();
            if (!text || this.loading) return;
            const cid = this.activeId;

            this.messages.push({ role: 'user', content: text, created_at: new Date().toISOString() });
            const body = {
                messages: this.messages.map(m => ({ role: m.role, content: m.content })),
                conversation_id: cid,
            };
            this.draft = '';
            this.loading = true;
            this.streamStarted = false;
            this.streamError = null;
            this.toolActivity = [];
            this.summary = null;
            this.scrollChatBottom();

            const assistant = { role: 'assistant', content: '', created_at: new Date().toISOString() };
            this.messages.push(assistant);
            let gotDone = false;

            try {
                const resp = await AM.fetch('/plugins/chat/stream', { method: 'POST', body });
                if (!resp || !resp.ok) {
                    let detail = '';
                    if (resp && resp.json) {
                        const err = await resp.json().catch(() => ({}));
                        detail = err.detail || '';
                    }
                    throw new Error(detail || 'Failed to send message');
                }
                if (!resp.body) throw new Error('Streaming not supported');

                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });
                    let sep;
                    while ((sep = buffer.indexOf('\n\n')) >= 0) {
                        const rawEvent = buffer.slice(0, sep);
                        buffer = buffer.slice(sep + 2);
                        const dataLine = rawEvent.split('\n').find(l => l.startsWith('data: '));
                        if (!dataLine) continue;
                        let ev;
                        try { ev = JSON.parse(dataLine.slice(6)); } catch (e) { continue; }
                        if (ev.type === 'text') {
                            this.streamStarted = true;
                            assistant.content += ev.content;
                            this.scrollChatBottom();
                        } else if (ev.type === 'tool_call') {
                            this.streamStarted = true;
                            for (const name of ev.tools || []) {
                                if (!this.toolActivity.find(t => t.name === name)) {
                                    this.toolActivity.push({ name, result: null });
                                }
                            }
                            this.scrollChatBottom();
                        } else if (ev.type === 'tool_result') {
                            const entry = this.toolActivity.find(t => t.name === ev.tool);
                            if (entry) {
                                entry.result = typeof ev.result === 'string' ? ev.result : JSON.stringify(ev.result);
                            }
                        } else if (ev.type === 'done') {
                            gotDone = true;
                            if (!cid && ev.conversation_id) this.activeId = ev.conversation_id;
                            if (!cid || ev.title_updated) await this.refreshConversationList();
                        }
                    }
                }
                if (!gotDone) throw new Error('Stream ended unexpectedly');
                if (!assistant.content && this.toolActivity.length === 0) {
                    const idx = this.messages.indexOf(assistant);
                    if (idx > -1) this.messages.splice(idx, 1);
                }
            } catch (e) {
                if (assistant.content) {
                    this.streamError = 'Response interrupted — partial reply shown';
                    AM.toast('Stream interrupted', 'error');
                } else {
                    const idx = this.messages.indexOf(assistant);
                    if (idx > -1) this.messages.splice(idx, 1);
                    this.streamError = e.message || 'Failed to send message';
                    AM.toast(e.message || 'Failed to send message', 'error');
                }
            } finally {
                this.loading = false;
                this.scrollChatBottom();
            }
        },

        async refreshConversationList() {
            let url = '/plugins/chat/conversations';
            if (this.showArchived) url += '?archived=true';
            const resp = await AM.fetch(url);
            if (!resp || !resp.ok) return;
            const convs = await resp.json();
            this.conversations = convs;
            if (!this.activeId && convs.length > 0) {
                this.activeId = convs[0].id;
                this.activeTitle = convs[0].title || '';
            } else {
                const updated = convs.find(c => c.id === this.activeId);
                if (updated) this.activeTitle = updated.title || '';
            }
        },

        async summarizeConversation() {
            if (!this.activeId || this.summarizing) return;
            this.summarizing = true;
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + this.activeId + '/summarize', { method: 'POST' });
                if (!resp || !resp.ok) {
                    let detail = '';
                    if (resp && resp.json) {
                        const err = await resp.json().catch(() => ({}));
                        detail = err.detail || '';
                    }
                    throw new Error(detail || 'Summarize failed');
                }
                const data = await resp.json();
                this.summary = { text: data.summary || '', created_at: new Date().toISOString() };
                this.scrollChatBottom();
            } catch (e) {
                AM.toast(e.message || 'Summarize failed', 'error');
            } finally {
                this.summarizing = false;
            }
        },

        startThreadEdit(conv) {
            this.editingThreadId = conv.id;
            this.editTitleDraft = conv.title || '';
            this.$nextTick(() => {
                const el = this.$root && this.$root.querySelector('.thread-rename-input');
                if (el) { el.focus(); el.select(); }
            });
        },

        cancelThreadEdit() {
            this.editingThreadId = null;
            this.editTitleDraft = '';
        },

        async saveThreadEdit() {
            if (this.editingThreadId === null) return;
            const id = this.editingThreadId;
            const title = this.editTitleDraft.trim();
            this.editingThreadId = null;
            this.editTitleDraft = '';
            if (!title) return;
            const conv = this.conversations.find(c => c.id === id);
            const oldTitle = conv ? conv.title : '';
            if (conv) conv.title = title;
            if (this.activeId === id) this.activeTitle = title;
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + id, { method: 'PUT', body: { title } });
                if (!resp || !resp.ok) {
                    let detail = '';
                    if (resp && resp.json) {
                        const err = await resp.json().catch(() => ({}));
                        detail = err.detail || '';
                    }
                    throw new Error(detail || 'Rename failed');
                }
            } catch (e) {
                if (conv) conv.title = oldTitle;
                if (this.activeId === id) this.activeTitle = oldTitle;
                AM.toast(e.message || 'Rename failed', 'error');
            }
        },

        async repromptMessage(index) {
            const newContent = this.messages[index]._editText;
            const cid = this.activeId;
            if (!cid) return;

            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + cid + '/messages/' + index, {
                    method: 'PUT',
                    body: { content: newContent },
                });
                if (!resp || !resp.ok) throw new Error('Failed to update message');
                const detailResp = await AM.fetch('/plugins/chat/conversations/' + cid);
                if (detailResp && detailResp.ok) {
                    const detail = await detailResp.json();
                    this.messages = detail.messages.map(m => ({ role: m.role, content: m.content, _editText: '', created_at: m.created_at }));
                }
                this.editingIndex = null;
                this.scrollChatBottom();
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        scrollChatBottom() {
            this.$nextTick(() => {
                const el = this.$refs.chatMessages;
                if (!el) return;
                const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
                if (isNearBottom) el.scrollTop = el.scrollHeight;
            });
        },

        async searchConversations(query) {
            if (this.searchTimer) clearTimeout(this.searchTimer);
            this.searchTimer = setTimeout(async () => {
                try {
                    let url = '/plugins/chat/conversations';
                    if (query.trim()) url += '?q=' + encodeURIComponent(query);
                    if (this.showArchived) url += (query.trim() ? '&' : '?') + 'archived=true';
                    const resp = await AM.fetch(url);
                    if (!resp || !resp.ok) {
                        AM.toast('Search failed', 'error');
                        return;
                    }
                    this.conversations = await resp.json();
                } catch (e) {
                    console.error('Chat searchConversations', e);
                    AM.toast('Search failed', 'error');
                }
            }, 300);
        },

        toggleArchived() {
            this.showArchived = !this.showArchived;
            this.loadConversations();
        },

        async deleteConversation(id) {
            if (!confirm('Delete this conversation permanently? Extracted memory facts will be kept.')) return;
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + id, { method: 'DELETE' });
                if (!resp || !resp.ok) throw new Error('Failed to delete conversation');
                this.conversations = this.conversations.filter(c => c.id !== id);
                if (this.activeId === id) {
                    this.activeId = null;
                    this.activeTitle = '';
                    this.messages = [];
                    this.summary = null;
                    if (this.conversations.length > 0) {
                        await this.loadConversation(this.conversations[0].id);
                    }
                }
                AM.toast('Conversation deleted', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async archiveConversation(id) {
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + id + '/archive', { method: 'POST' });
                if (!resp || !resp.ok) throw new Error('Failed to archive conversation');
                this.conversations = this.conversations.filter(c => c.id !== id);
                if (this.activeId === id) {
                    this.activeId = null;
                    this.activeTitle = '';
                    this.messages = [];
                    this.summary = null;
                    if (this.conversations.length > 0) {
                        await this.loadConversation(this.conversations[0].id);
                    }
                }
                AM.toast('Conversation archived', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async unarchiveConversation(id) {
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + id + '/unarchive', { method: 'POST' });
                if (!resp || !resp.ok) throw new Error('Failed to unarchive conversation');
                this.conversations = this.conversations.filter(c => c.id !== id);
                if (this.activeId === id) {
                    this.activeId = null;
                    this.activeTitle = '';
                    this.messages = [];
                    this.summary = null;
                    if (this.conversations.length > 0) {
                        await this.loadConversation(this.conversations[0].id);
                    }
                }
                AM.toast('Conversation unarchived', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async regenerateResponse() {
            const cid = this.activeId;
            if (!cid || this.loading) return;
            let lastIndex = this.messages.length - 1;
            while (lastIndex >= 0 && this.messages[lastIndex].role !== 'user') lastIndex--;
            if (lastIndex < 0) return;
            const userContent = this.messages[lastIndex].content;
            this.loading = true;
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + cid + '/messages/' + lastIndex, {
                    method: 'PUT',
                    body: { content: userContent },
                });
                if (!resp || !resp.ok) throw new Error('Failed to regenerate response');
                const detailResp = await AM.fetch('/plugins/chat/conversations/' + cid);
                if (detailResp && detailResp.ok) {
                    const detail = await detailResp.json();
                    this.messages = detail.messages.map(m => ({ role: m.role, content: m.content, _editText: '', created_at: m.created_at }));
                }
                this.scrollChatBottom();
            } catch (e) { AM.toast(e.message, 'error'); }
            finally { this.loading = false; }
        },

        copyMessage(content) {
            navigator.clipboard.writeText(content).then(() => {
                AM.toast('Copied', 'success');
            }).catch(() => {
                AM.toast('Copy failed', 'error');
            });
        },

        async speakText(text) {
            if (AM.tts) {
                AM.tts.speak(text, 'alloy');
            } else {
                try {
                    const resp = await AM.fetch('/plugins/tts', { method: 'POST', body: { text, voice: 'alloy' } });
                    if (!resp.ok) {
                        const e = await resp.json().catch(() => ({}));
                        if (resp.status === 503) { AM.toast('TTS not configured', 'warning'); return; }
                        AM.toast(e.detail || 'TTS failed', 'error');
                        return;
                    }
                    const blob = await resp.blob();
                    const audio = new Audio(URL.createObjectURL(blob));
                    audio.play();
                } catch (e) { AM.toast('TTS error: ' + e.message, 'error'); }
            }
        },

        dayLabel(iso) {
            if (!iso) return '';
            const d = new Date(iso);
            if (isNaN(d)) return '';
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
            const diffDays = Math.round((today - that) / 86400000);
            if (diffDays === 0) return 'Today';
            if (diffDays === 1) return 'Yesterday';
            return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined });
        },

        showDaySeparator(i) {
            if (i === 0) return true;
            const cur = this.messages[i] && this.messages[i].created_at;
            const prev = this.messages[i - 1] && this.messages[i - 1].created_at;
            if (!cur || !prev) return false;
            return new Date(cur).toDateString() !== new Date(prev).toDateString();
        },

        msgTime(iso) {
            if (!iso) return '';
            const d = new Date(iso);
            if (isNaN(d)) return '';
            return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
        },

        handleChatClick(e) {
            const target = e.target;
            if (target && target.classList && target.classList.contains('wikilink')) {
                if (window.AM && AM.notes && typeof AM.notes.open === 'function') {
                    AM.notes.open(target.textContent);
                }
            }
        },
    };
}
