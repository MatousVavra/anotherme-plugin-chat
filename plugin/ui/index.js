function chatPlugin() {
    return {
        conversations: [],
        activeId: null,
        activeTitle: '',
        messages: [],
        draft: '',
        loading: false,
        editingIndex: null,
        searchQuery: '',
        showArchived: false,
        searchTimer: null,
        mobileThreadSheetOpen: false,

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
            try {
                let url = '/plugins/chat/conversations';
                if (this.showArchived) url += '?archived=true';
                const resp = await AM.fetch(url);
                if (!resp) return;
                this.conversations = await resp.json();
                if (this.conversations.length > 0) {
                    await this.loadConversation(this.conversations[0].id);
                }
            } catch (e) { console.error('Chat loadConversations', e); }
        },

        async loadConversation(id) {
            this.activeId = id;
            this.messages = [];
            this.editingIndex = null;
            try {
                const resp = await AM.fetch('/plugins/chat/conversations/' + id);
                if (!resp) return;
                const detail = await resp.json();
                this.activeTitle = detail.title || '';
                this.messages = detail.messages.map(m => ({ role: m.role, content: m.content, _editText: '', created_at: m.created_at }));
            } catch (e) { console.error('Chat loadConversation', e); }
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
                if (!resp) return;
                const conv = await resp.json();
                this.conversations.unshift(conv);
                this.activeId = conv.id;
                this.activeTitle = conv.title || '';
                this.messages = [];
                this.editingIndex = null;
                this.$nextTick(() => { const el = this.$refs.chatInput; if (el) el.focus(); });
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async sendChatMessage() {
            const text = this.draft.trim();
            if (!text) return;
            const cid = this.activeId;

            this.messages.push({ role: 'user', content: text, created_at: new Date().toISOString() });
            this.draft = '';
            this.loading = true;
            this.scrollChatBottom();

            try {
                const body = {
                    messages: this.messages.map(m => ({ role: m.role, content: m.content })),
                    conversation_id: cid,
                };
                const resp = await AM.fetch('/plugins/chat/send', { method: 'POST', body });
                if (!resp) return;
                const data = await resp.json();
                this.messages.push({ role: 'assistant', content: data.reply || '', created_at: new Date().toISOString() });

                if (!cid || data.title_updated) {
                    let url = '/plugins/chat/conversations';
                    if (this.showArchived) url += '?archived=true';
                    const r2 = await AM.fetch(url);
                    if (r2) {
                        const convs = await r2.json();
                        this.conversations = convs;
                        if (!cid && convs.length > 0) {
                            this.activeId = convs[0].id;
                            this.activeTitle = convs[0].title || '';
                        } else {
                            const updated = convs.find(c => c.id === this.activeId);
                            if (updated) this.activeTitle = updated.title || '';
                        }
                    }
                }
            } catch (e) {
                this.messages.push({ role: 'assistant', content: 'Error: ' + e.message, created_at: new Date().toISOString() });
            } finally {
                this.loading = false;
                this.scrollChatBottom();
            }
        },

        async repromptMessage(index) {
            const newContent = this.messages[index]._editText;
            const cid = this.activeId;
            if (!cid) return;

            try {
                await AM.fetch('/plugins/chat/conversations/' + cid + '/messages/' + index, {
                    method: 'PUT',
                    body: { content: newContent },
                });
                const resp = await AM.fetch('/plugins/chat/conversations/' + cid);
                if (resp) {
                    const detail = await resp.json();
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
                    if (!query.trim()) {
                        let url = '/plugins/chat/conversations';
                        if (this.showArchived) url += '?archived=true';
                        const resp = await AM.fetch(url);
                        if (resp) this.conversations = await resp.json();
                    } else {
                        let url = '/plugins/chat/conversations?q=' + encodeURIComponent(query);
                        if (this.showArchived) url += '&archived=true';
                        const resp = await AM.fetch(url);
                        if (resp) this.conversations = await resp.json();
                    }
                } catch (e) { console.error('Chat searchConversations', e); }
            }, 300);
        },

        toggleArchived() {
            this.showArchived = !this.showArchived;
            this.loadConversations();
        },

        async deleteConversation(id) {
            if (!confirm('Delete this conversation permanently? Extracted memory facts will be kept.')) return;
            try {
                await AM.fetch('/plugins/chat/conversations/' + id, { method: 'DELETE' });
                this.conversations = this.conversations.filter(c => c.id !== id);
                if (this.activeId === id) {
                    this.activeId = null;
                    this.activeTitle = '';
                    this.messages = [];
                    if (this.conversations.length > 0) {
                        await this.loadConversation(this.conversations[0].id);
                    }
                }
                AM.toast('Conversation deleted', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async archiveConversation(id) {
            try {
                await AM.fetch('/plugins/chat/conversations/' + id + '/archive', { method: 'POST' });
                this.conversations = this.conversations.filter(c => c.id !== id);
                if (this.activeId === id) {
                    this.activeId = null;
                    this.activeTitle = '';
                    this.messages = [];
                    if (this.conversations.length > 0) {
                        await this.loadConversation(this.conversations[0].id);
                    }
                }
                AM.toast('Conversation archived', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async unarchiveConversation(id) {
            try {
                await AM.fetch('/plugins/chat/conversations/' + id + '/unarchive', { method: 'POST' });
                this.conversations = this.conversations.filter(c => c.id !== id);
                if (this.activeId === id) {
                    this.activeId = null;
                    this.activeTitle = '';
                    this.messages = [];
                    if (this.conversations.length > 0) {
                        await this.loadConversation(this.conversations[0].id);
                    }
                }
                AM.toast('Conversation unarchived', 'success');
            } catch (e) { AM.toast(e.message, 'error'); }
        },

        async regenerateResponse() {
            const cid = this.activeId;
            if (!cid) return;
            let lastIndex = this.messages.length - 1;
            while (lastIndex >= 0 && this.messages[lastIndex].role !== 'user') lastIndex--;
            if (lastIndex < 0) return;
            const userContent = this.messages[lastIndex].content;
            this.loading = true;
            try {
                await AM.fetch('/plugins/chat/conversations/' + cid + '/messages/' + lastIndex, {
                    method: 'PUT',
                    body: { content: userContent },
                });
                const resp = await AM.fetch('/plugins/chat/conversations/' + cid);
                if (resp) {
                    const detail = await resp.json();
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
    };
}
