import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AnnouncementsPage } from './AnnouncementsPage';
import { AnnouncementBanners } from '@/components/layout/AnnouncementBanners';
import { useAnnouncementsStore } from '@/store/announcements';
import { useUserStore } from '@/store/admin';
import { ApiError } from '@/services/api';
import * as adminApi from '@/services/adminApi';
import type { AnnouncementAdmin, AnnouncementWrite, User } from '@/types/admin';

vi.mock('@/services/adminApi');

const ROW: AnnouncementAdmin = {
  id: 'a1',
  message: 'Maintenance tonight',
  level: 'warning',
  color: null,
  text_color: null,
  opacity: 100,
  dismissible: true,
  enabled: true,
  ends_at: null,
  updated_at: '2026-10-28T10:00:00Z',
  created_by: 'u1',
  created_at: '2026-10-28T09:00:00Z',
};

const WRITE_FIELDS = ['message', 'level', 'color', 'text_color', 'opacity', 'dismissible', 'enabled', 'ends_at'] as const;
const writeOf = (a: AnnouncementAdmin): AnnouncementWrite =>
  Object.fromEntries(WRITE_FIELDS.map((k) => [k, a[k]])) as AnnouncementWrite;

const refresh = vi.fn(async () => {});

const renderPage = async (rows: AnnouncementAdmin[] = [ROW]) => {
  vi.mocked(adminApi.getAdminAnnouncements).mockResolvedValue(rows);
  render(<AnnouncementsPage />);
  await waitFor(() => expect(adminApi.getAdminAnnouncements).toHaveBeenCalled());
  if (rows.length) await screen.findAllByRole('button', { name: /^Edit announcement/ });
};

const dialog = () => screen.getByRole('dialog');
const message = () => screen.getByLabelText('Message') as HTMLTextAreaElement;
const endsAt = () => screen.getByLabelText(/Ends at/) as HTMLInputElement;
const preview = () => screen.getByTestId('announcement-preview').firstElementChild as HTMLElement;
const rowBar = (i = 1) => screen.getAllByRole('row')[i].querySelector('[data-level]') as HTMLElement;
// A promise the test settles by hand, to observe the page while a request is in flight.
const deferred = <T,>() => {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
};
const lastPut = () => vi.mocked(adminApi.updateAnnouncement).mock.calls.at(-1)!;
const lastPost = () => vi.mocked(adminApi.createAnnouncement).mock.calls.at(-1)![0];
// Independent of the page's formatter: sv-SE prints local time as 'YYYY-MM-DD HH:mm:ss'.
const localInputOf = (iso: string) => new Date(iso).toLocaleString('sv-SE').slice(0, 16).replace(' ', 'T');

// After a write the list reloads and this tab's banner stack refetches (004-AC3).
const expectRefreshedAfterWrite = async () => {
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  expect(adminApi.getAdminAnnouncements).toHaveBeenCalledTimes(2);
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  useAnnouncementsStore.setState({ items: [], loaded: false, refresh });
  useUserStore.setState({ users: [], error: null });
  vi.mocked(adminApi.getUsers).mockResolvedValue([]);
  vi.mocked(adminApi.createAnnouncement).mockResolvedValue(ROW);
  vi.mocked(adminApi.updateAnnouncement).mockResolvedValue(ROW);
  vi.mocked(adminApi.deleteAnnouncement).mockResolvedValue(undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AnnouncementsPage', () => {
  it('lists each banner with its preview, level, close mode, end date and expired marker', async () => {
    const past = { ...ROW, id: 'p', message: 'Old news', dismissible: false, ends_at: '2020-01-01T00:00:00Z' };
    const future = { ...ROW, id: 'f', message: 'Soon', ends_at: '2999-01-01T00:00:00Z' };
    await renderPage([ROW, past, future]);
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell')[0].textContent)).toEqual(['Maintenance tonight', 'Old news', 'Soon']);
    expect(within(rows[0]).getByText('warning')).toBeInTheDocument();
    expect(within(rows[0]).getByText('Dismissible')).toBeInTheDocument();
    expect(within(rows[0]).getByText('never')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Pinned')).toBeInTheDocument();
    expect(within(rows[1]).getByText(new Date(past.ends_at).toLocaleString(), { exact: false })).toBeInTheDocument();
    expect(within(rows[1]).getByText('expired')).toBeInTheDocument();
    expect(within(rows[2]).queryByText('expired')).toBeNull();
  });

  describe('author', () => {
    const USER = { id: 'u1', display_name: 'Ada Admin', email: 'ada@example.com' } as User;
    const authorCell = () => within(screen.getAllByRole('row')[1]).getAllByRole('cell')[6];

    it("shows the author's display name, or the email when it has none", async () => {
      vi.mocked(adminApi.getUsers).mockResolvedValue([USER, { ...USER, id: 'u2', display_name: '', email: 'bob@example.com' } as User]);
      await renderPage([ROW, { ...ROW, id: 'a2', created_by: 'u2' }]);
      await waitFor(() => expect(authorCell()).toHaveTextContent('Ada Admin'));
      expect(within(screen.getAllByRole('row')[2]).getAllByRole('cell')[6]).toHaveTextContent('bob@example.com');
    });

    it('shows "deleted user" when created_by is null', async () => {
      vi.mocked(adminApi.getUsers).mockResolvedValue([USER]);
      await renderPage([{ ...ROW, created_by: null }]);
      await waitFor(() => expect(adminApi.getUsers).toHaveBeenCalled());
      expect(authorCell()).toHaveTextContent('deleted user');
    });

    it('shows the short id when the author is not in the user list', async () => {
      vi.mocked(adminApi.getUsers).mockResolvedValue([USER]);
      await renderPage([{ ...ROW, created_by: '9f8e7d6c-0000-4000-8000-000000000000' }]);
      await waitFor(() => expect(adminApi.getUsers).toHaveBeenCalled());
      expect(authorCell().textContent).toBe('9f8e7d6c');
    });

    it('falls back to the short id when the users fetch fails, and the page still works', async () => {
      vi.mocked(adminApi.getUsers).mockRejectedValue(new ApiError(500, 'users down'));
      await renderPage([{ ...ROW, created_by: '9f8e7d6c-0000-4000-8000-000000000000' }]);
      await waitFor(() => expect(adminApi.getUsers).toHaveBeenCalled());
      expect(authorCell().textContent).toBe('9f8e7d6c');
      expect(screen.queryByText('users down')).toBeNull();
      await userEvent.click(screen.getByRole('switch'));
      await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(1));
    });
  });

  it('004-AC3: creates a banner with the full body, then reloads the list and the banner stack', async () => {
    await renderPage();
    await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
    await userEvent.type(message(), 'Bedrock is **throttled**');
    await userEvent.selectOptions(screen.getByLabelText('Level'), 'danger');
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() => expect(adminApi.createAnnouncement).toHaveBeenCalledTimes(1));
    expect(lastPost()).toEqual({
      message: 'Bedrock is **throttled**',
      level: 'danger',
      color: null,
      text_color: null,
      opacity: 100,
      dismissible: true,
      enabled: true,
      ends_at: null,
    });
    await expectRefreshedAfterWrite();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('004-AC3: edits a banner with a full PUT, then refreshes', async () => {
    const row = { ...ROW, color: '#123456', text_color: '#fefefe', opacity: 40, dismissible: false, ends_at: '2999-06-01T08:30:00Z' };
    await renderPage([row]);
    await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
    expect(message().value).toBe('Maintenance tonight');
    await userEvent.clear(message());
    await userEvent.type(message(), 'Done');
    await userEvent.click(screen.getByRole('button', { name: /^update$/i }));

    await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(1));
    const [id, body] = lastPut();
    expect(id).toBe('a1');
    expect({ ...body, ends_at: Date.parse(body.ends_at!) }).toEqual({
      ...writeOf(row),
      message: 'Done',
      ends_at: Date.parse(row.ends_at),
    });
    await expectRefreshedAfterWrite();
  });

  it('004-AC3: deletes after confirmation only, then refreshes', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    await renderPage();
    await userEvent.click(screen.getByRole('button', { name: /^Delete announcement/ }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(adminApi.deleteAnnouncement).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: /^Delete announcement/ }));
    await waitFor(() => expect(adminApi.deleteAnnouncement).toHaveBeenCalledWith('a1'));
    await expectRefreshedAfterWrite();
  });

  it.each([true, false])('004-AC3: the enabled toggle (from %s) sends the full body with enabled flipped', async (enabled) => {
    const row = { ...ROW, enabled, color: '#abcdef', opacity: 55, ends_at: '2020-01-01T00:00:00Z' };
    await renderPage([row]);
    await userEvent.click(screen.getByRole('switch'));
    await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(1));
    expect(lastPut()).toEqual(['a1', { ...writeOf(row), enabled: !enabled }]);
    await expectRefreshedAfterWrite();
  });

  it('shows a failed toggle or delete in the page error banner', async () => {
    vi.mocked(adminApi.deleteAnnouncement).mockRejectedValue(new ApiError(404, 'Announcement not found'));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await renderPage();
    await userEvent.click(screen.getByRole('button', { name: /^Delete announcement/ }));
    expect(await screen.findByText('Announcement not found')).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

  describe('one write at a time', () => {
    it('a second Create click while the POST is in flight sends nothing', async () => {
      const post = deferred<AnnouncementAdmin>();
      vi.mocked(adminApi.createAnnouncement).mockReturnValue(post.promise);
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      await userEvent.type(message(), 'Once');
      const create = screen.getByRole('button', { name: /^create$/i });
      await userEvent.click(create);
      // Busy but still enabled: disabling the focused button would drop focus to <body>.
      expect(create).toHaveAttribute('aria-disabled', 'true');
      expect(create).not.toBeDisabled();
      expect(create).toHaveFocus();
      await userEvent.click(create);
      fireEvent.submit(create.closest('form')!); // Enter in a field, past the disabled button
      expect(adminApi.createAnnouncement).toHaveBeenCalledTimes(1);
      post.resolve(ROW);
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    });

    it('while the POST is in flight the fields are locked and a submit shows no validation error', async () => {
      const post = deferred<AnnouncementAdmin>();
      vi.mocked(adminApi.createAnnouncement).mockReturnValue(post.promise);
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      await userEvent.type(message(), 'First');
      await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
      // The form closes on success: an edit typed meanwhile would be dropped without notice.
      expect(message()).toBeDisabled();
      expect(within(dialog()).getByRole('combobox')).toBeDisabled();
      fireEvent.change(message(), { target: { value: '' } });
      fireEvent.submit(message().closest('form')!);
      expect(screen.queryByRole('alert')).toBeNull();
      post.resolve(ROW);
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(adminApi.createAnnouncement).mock.calls.map(([body]) => body.message)).toEqual(['First']);
    });

    it('a second switch click while the PUT is in flight sends nothing', async () => {
      const put = deferred<AnnouncementAdmin>();
      vi.mocked(adminApi.updateAnnouncement).mockReturnValue(put.promise);
      vi.spyOn(window, 'confirm').mockReturnValue(true);
      await renderPage();
      await userEvent.click(screen.getByRole('switch'));
      const toggle = screen.getByRole('switch');
      expect(toggle).toHaveAttribute('aria-disabled', 'true');
      expect(toggle).not.toBeDisabled();
      expect(toggle).toHaveFocus();
      expect(screen.getByRole('button', { name: /^Delete announcement/ })).toHaveAttribute('aria-disabled', 'true');
      await userEvent.click(screen.getByRole('switch'));
      expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(1);
      put.resolve(ROW);
      await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-disabled', 'false'));
    });
  });

  it('a delete in flight keeps its focused button enabled but busy; clicks meanwhile do nothing', async () => {
    const del = deferred<void>();
    vi.mocked(adminApi.deleteAnnouncement).mockReturnValue(del.promise);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await renderPage();
    const button = screen.getByRole('button', { name: /^Delete announcement/ });
    await userEvent.click(button);
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(button).not.toBeDisabled();
    expect(button).toHaveFocus();
    await userEvent.click(button);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(adminApi.deleteAnnouncement).toHaveBeenCalledTimes(1);
    del.resolve();
    await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'false'));
  });

  it('a failed toggle error clears once the admin opens the form, and stays cleared after the save', async () => {
    vi.mocked(adminApi.updateAnnouncement).mockRejectedValueOnce(new ApiError(404, 'Announcement not found'));
    await renderPage();
    await userEvent.click(screen.getByRole('switch'));
    expect(await screen.findByText('Announcement not found')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
    expect(screen.queryByText('Announcement not found')).toBeNull();
    await userEvent.type(message(), 'Fresh');
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByText('Announcement not found')).toBeNull();
  });

  describe('no form opens over a write in flight', () => {
    it('a toggle in flight disables Edit and Add; editing afterwards starts from the toggled row', async () => {
      const put = deferred<AnnouncementAdmin>();
      vi.mocked(adminApi.updateAnnouncement).mockReturnValueOnce(put.promise);
      await renderPage();
      await userEvent.click(screen.getByRole('switch'));
      const edit = screen.getByRole('button', { name: /^Edit announcement/ });
      expect(edit).toBeDisabled();
      expect(screen.getByRole('button', { name: /add announcement/i })).toBeDisabled();
      await userEvent.click(edit);
      expect(screen.queryByRole('dialog')).toBeNull();

      vi.mocked(adminApi.getAdminAnnouncements).mockResolvedValue([{ ...ROW, enabled: false }]);
      put.resolve({ ...ROW, enabled: false });
      await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false'));
      await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
      await userEvent.click(screen.getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(2));
      expect(vi.mocked(adminApi.updateAnnouncement).mock.calls.map(([, body]) => body.enabled)).toEqual([false, false]);
    });

    it('the form cannot close while its write is in flight, and a late 422 shows in it', async () => {
      let reject!: (e: unknown) => void;
      vi.mocked(adminApi.createAnnouncement).mockReturnValue(new Promise((_res, rej) => (reject = rej)));
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      await userEvent.type(message(), 'Slow');
      await userEvent.click(screen.getByRole('button', { name: /^create$/i }));

      expect(screen.getByRole('button', { name: /cancel/i })).toBeDisabled();
      await userEvent.keyboard('{Escape}');
      expect(dialog()).toBeInTheDocument();
      const header = within(dialog()).getAllByRole('button')[0]; // the Modal's X
      await userEvent.click(header);
      expect(dialog()).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /add announcement/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /^Edit announcement/ })).toBeDisabled();

      reject(new ApiError(422, JSON.stringify([{ loc: ['body', 'message'], msg: 'Too long' }])));
      expect(await screen.findByRole('alert')).toHaveTextContent('message: Too long');
      expect(message().value).toBe('Slow');
      expect(screen.getByRole('button', { name: /cancel/i })).not.toBeDisabled();
      await userEvent.keyboard('{Escape}');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    });
  });

  it('reloads the list after a failed write: a row another admin deleted disappears, the error stays', async () => {
    vi.mocked(adminApi.updateAnnouncement).mockRejectedValue(new ApiError(404, 'Announcement not found'));
    await renderPage();
    vi.mocked(adminApi.getAdminAnnouncements).mockResolvedValue([]);
    await userEvent.click(screen.getByRole('switch'));
    await waitFor(() => expect(screen.queryByRole('switch')).toBeNull());
    expect(adminApi.getAdminAnnouncements).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Announcement not found')).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('keeps the rows (and the focused switch) mounted while a post-write reload is in flight', async () => {
    await renderPage();
    const reload = deferred<AnnouncementAdmin[]>();
    vi.mocked(adminApi.getAdminAnnouncements).mockReturnValue(reload.promise);
    const toggle = screen.getByRole('switch');
    await userEvent.click(toggle);
    await waitFor(() => expect(adminApi.getAdminAnnouncements).toHaveBeenCalledTimes(2));
    expect(toggle).toBeInTheDocument();
    expect(toggle).toHaveFocus();
    reload.resolve([{ ...ROW, enabled: false }]);
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'));
    expect(toggle).toBeInTheDocument();
  });

  it('refetches the list when the tab becomes visible again, not when hidden or after unmount', async () => {
    const setVisibility = (v: DocumentVisibilityState) => {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(v);
      document.dispatchEvent(new Event('visibilitychange'));
    };
    vi.mocked(adminApi.getAdminAnnouncements).mockResolvedValue([ROW]);
    const { unmount } = render(<AnnouncementsPage />);
    await waitFor(() => expect(adminApi.getAdminAnnouncements).toHaveBeenCalledTimes(1));
    setVisibility('hidden');
    expect(adminApi.getAdminAnnouncements).toHaveBeenCalledTimes(1);
    setVisibility('visible');
    await waitFor(() => expect(adminApi.getAdminAnnouncements).toHaveBeenCalledTimes(2));
    unmount();
    setVisibility('visible');
    expect(adminApi.getAdminAnnouncements).toHaveBeenCalledTimes(2);
  });

  describe('row labels', () => {
    const created = (iso: string) => `(created ${new Date(iso).toLocaleString()})`;

    it('name each action by its message text and creation time', async () => {
      const long = 'x'.repeat(60);
      await renderPage([ROW, { ...ROW, id: 'a2', message: long, enabled: false }]);
      const suffix = created(ROW.created_at);
      expect(screen.getByRole('button', { name: `Edit announcement: Maintenance tonight ${suffix}` })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: `Delete announcement: ${'x'.repeat(40)} ${suffix}` })).toBeInTheDocument();
      expect(screen.getByRole('switch', { name: `Enabled: Maintenance tonight ${suffix}` })).toBeChecked();
      expect(screen.getByRole('switch', { name: `Enabled: ${'x'.repeat(40)} ${suffix}` })).not.toBeChecked();
    });

    it('stay distinct for two banners sharing a prefix, and carry no markdown syntax', async () => {
      const md = '**Bedrock** is _throttled_ in `eu-west-1`, see [status](https://status.example.com) for more';
      await renderPage([
        { ...ROW, id: 'a1', message: md, created_at: '2026-10-28T09:00:00Z' },
        { ...ROW, id: 'a2', message: `${md} (update)`, created_at: '2026-10-28T11:30:00Z' },
      ]);
      const names = ['Edit announcement', 'Delete announcement'].flatMap((n) =>
        screen.getAllByRole('button', { name: new RegExp(`^${n}`) }).map((b) => b.getAttribute('aria-label')!),
      );
      names.push(...screen.getAllByRole('switch').map((b) => b.getAttribute('aria-label')!));
      expect(new Set(names).size).toBe(6);
      for (const name of names) {
        for (const syntax of ['**', '_', '`', '[', '](']) expect(name).not.toContain(syntax);
      }
      expect(names[0]).toBe(`Edit announcement: Bedrock is throttled in eu-west-1, see s ${created('2026-10-28T09:00:00Z')}`);
    });

    it("keep the switch's name when its state flips; the state lives in aria-checked", async () => {
      await renderPage();
      const toggle = screen.getByRole('switch');
      const name = toggle.getAttribute('aria-label');
      expect(name).toMatch(/^Enabled: Maintenance tonight/);
      expect(toggle).toHaveAttribute('aria-checked', 'true');
      vi.mocked(adminApi.getAdminAnnouncements).mockResolvedValue([{ ...ROW, enabled: false }]);
      await userEvent.click(toggle);
      await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false'));
      expect(screen.getByRole('switch').getAttribute('aria-label')).toBe(name);
    });
  });

  describe('422 display', () => {
    const openAndType = async (text: string) => {
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      await userEvent.type(message(), text);
      fireEvent.change(endsAt(), { target: { value: '2026-11-01T09:30' } });
      await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
    };

    it('lists the pydantic messages by field, keeps the form open and the input intact', async () => {
      const detail = [
        { type: 'value_error', loc: ['body', 'ends_at'], msg: 'Value error, ends_at must be in the future' },
        { type: 'string_pattern_mismatch', loc: ['body', 'color'], msg: "String should match pattern '^#[0-9a-fA-F]{6}$'" },
      ];
      vi.mocked(adminApi.createAnnouncement).mockRejectedValue(new ApiError(422, JSON.stringify(detail)));
      await openAndType('Keep me');

      const alert = await screen.findByRole('alert');
      expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
        'ends_at: Value error, ends_at must be in the future',
        "color: String should match pattern '^#[0-9a-fA-F]{6}$'",
      ]);
      expect(dialog()).toBeInTheDocument();
      expect(message().value).toBe('Keep me');
      expect(endsAt().value).toBe('2026-11-01T09:30');
      expect(refresh).not.toHaveBeenCalled();
    });

    it.each([
      ['a 422 with a string detail', new ApiError(422, 'Bad body')],
      ['a 500', new ApiError(500, 'boom')],
      ['a network error', new TypeError('Failed to fetch')],
    ])('shows %s as one message in the form', async (_label, err) => {
      vi.mocked(adminApi.createAnnouncement).mockRejectedValue(err);
      await openAndType('Keep me');
      expect((await screen.findByRole('alert')).textContent).toBe(err.message);
      expect(message().value).toBe('Keep me');
    });
  });

  describe('client-side guard and counter', () => {
    const openCreate = async () => {
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
    };
    const counter = () => screen.getByTestId('message-counter');

    it.each([
      ['surrounding spaces', '  hello  ', '5/500'],
      ['tabs and newlines', '\n\thello\n', '5/500'],
      ['an emoji (one code point, like the API)', ' hé🙂 ', '3/500'],
      ['only blanks', '   ', '0/500'],
    ])('counts the trimmed length with %s', async (_label, value, shown) => {
      await openCreate();
      fireEvent.change(message(), { target: { value } });
      expect(counter().textContent).toBe(shown);
    });

    it.each([
      ['blank', ' \n ', 'Message is required'],
      ['over 500 after trim', ` ${'x'.repeat(501)} `, 'At most 500 characters'],
    ])('blocks a %s message without calling the API', async (_label, value, text) => {
      await openCreate();
      fireEvent.change(message(), { target: { value } });
      await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
      expect(await screen.findByRole('alert')).toHaveTextContent(text);
      expect(adminApi.createAnnouncement).not.toHaveBeenCalled();
    });

    it('accepts exactly 500 characters once trimmed', async () => {
      await openCreate();
      fireEvent.change(message(), { target: { value: `  ${'x'.repeat(500)}  ` } });
      expect(counter().textContent).toBe('500/500');
      await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
      await waitFor(() => expect(adminApi.createAnnouncement).toHaveBeenCalledTimes(1));
    });
  });

  describe('end date (datetime-local <-> aware ISO)', () => {
    const AWARE = /(Z|[+-]\d{2}:\d{2})$/;

    it('sends the browser-local input as an aware instant', async () => {
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      await userEvent.type(message(), 'x');
      fireEvent.change(endsAt(), { target: { value: '2999-11-01T09:30' } });
      await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
      await waitFor(() => expect(adminApi.createAnnouncement).toHaveBeenCalled());
      const sent = lastPost().ends_at!;
      expect(sent).toMatch(AWARE);
      // Date(y, m, d, h, min) is local time by definition: no string parsing involved.
      expect(Date.parse(sent)).toBe(new Date(2999, 10, 1, 9, 30).getTime());
    });

    it.each(['2999-06-01T08:30:00Z', '2999-12-31T23:45:00+05:30', '2999-03-29T01:15:00-02:00'])(
      'shows %s in local time when editing and sends the same instant back',
      async (iso) => {
        await renderPage([{ ...ROW, ends_at: iso }]);
        await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
        expect(endsAt().value).toBe(localInputOf(iso));
        await userEvent.click(screen.getByRole('button', { name: /^update$/i }));
        await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalled());
        const sent = lastPut()[1].ends_at!;
        expect(sent).toMatch(AWARE);
        expect(Date.parse(sent)).toBe(Date.parse(iso));
      },
    );

    it('an untouched end date keeps its seconds; a changed one is sent at minute precision', async () => {
      const iso = '2999-06-01T08:30:45Z';
      await renderPage([{ ...ROW, ends_at: iso }]);
      await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
      await userEvent.click(screen.getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(1));
      expect(lastPut()[1].ends_at).toBe(iso);

      await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
      fireEvent.change(endsAt(), { target: { value: '2999-06-02T10:15' } });
      await userEvent.click(screen.getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalledTimes(2));
      expect(Date.parse(lastPut()[1].ends_at!)).toBe(new Date(2999, 5, 2, 10, 15).getTime());
    });

    it('a cleared field sends null', async () => {
      await renderPage([{ ...ROW, ends_at: '2999-06-01T08:30:00Z' }]);
      await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
      fireEvent.change(endsAt(), { target: { value: '' } });
      await userEvent.click(screen.getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalled());
      expect(lastPut()[1].ends_at).toBeNull();
    });
  });

  describe('colors', () => {
    const editAndSave = async (row: AnnouncementAdmin, act: () => Promise<void> | void) => {
      await renderPage([row]);
      await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
      await act();
      await userEvent.click(screen.getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(adminApi.updateAnnouncement).toHaveBeenCalled());
      return lastPut()[1];
    };

    it('"use level color" sends a null background, "automatic" a null text color', async () => {
      const body = await editAndSave({ ...ROW, color: '#ff0000', text_color: '#00ff00' }, async () => {
        expect(screen.getByLabelText('Background color')).not.toBeDisabled();
        await userEvent.click(screen.getByLabelText('Use level color'));
        await userEvent.click(screen.getByLabelText('Automatic text color'));
        expect(screen.getByLabelText('Background color')).toBeDisabled();
      });
      expect(body.color).toBeNull();
      expect(body.text_color).toBeNull();
    });

    it.each([
      ['an edited warning banner', { ...ROW, level: 'warning' as const }, null, '#f59e0b', 'rgb(245, 158, 11)'],
      ['a level changed in the form', ROW, 'danger', '#dc2626', 'rgb(220, 38, 38)'],
    ])('custom background starts from the level color for %s', async (_label, row, level, hex, rgb) => {
      const body = await editAndSave(row, async () => {
        if (level) await userEvent.selectOptions(screen.getByLabelText('Level'), level);
        await userEvent.click(screen.getByLabelText('Use level color'));
        expect(screen.getByLabelText('Background color')).toHaveValue(hex);
        expect(preview().style.backgroundColor).toBe(rgb);
      });
      expect(body.color).toBe(hex);
    });

    it('unticking them sends the picked colors', async () => {
      const body = await editAndSave(ROW, async () => {
        await userEvent.click(screen.getByLabelText('Use level color'));
        fireEvent.input(screen.getByLabelText('Background color'), { target: { value: '#112233' } });
        await userEvent.click(screen.getByLabelText('Automatic text color'));
        fireEvent.input(screen.getByLabelText('Text color'), { target: { value: '#ddeeff' } });
      });
      expect(body.color).toBe('#112233');
      expect(body.text_color).toBe('#ddeeff');
    });

    it('sends the opacity slider value and the dismissible / enabled boxes', async () => {
      const body = await editAndSave(ROW, async () => {
        fireEvent.change(screen.getByLabelText(/Opacity/), { target: { value: '35' } });
        expect(screen.getByText('Opacity: 35%')).toBeInTheDocument();
        await userEvent.click(screen.getByLabelText(/Users can close it/));
        await userEvent.click(screen.getByLabelText('Enabled'));
      });
      expect(body).toMatchObject({ opacity: 35, dismissible: false, enabled: false });
    });
  });

  describe('004-AC7: the preview is the real banner', () => {
    const snapshotOf = (el: HTMLElement) => ({
      style: el.getAttribute('style'),
      className: el.className,
      level: el.dataset.level,
      html: el.querySelector('span')!.innerHTML,
      // Close control (a button in the stack, an aria-hidden glyph in the console) after the text.
      closable: el.children.length === 2,
    });
    // The user-facing stack for the same announcement, rendered outside the page.
    const realBar = (a: AnnouncementAdmin) => {
      useAnnouncementsStore.setState({ items: [a], loaded: true, refresh });
      const { container, unmount } = render(<AnnouncementBanners />);
      const snapshot = snapshotOf(container.querySelector('[role="status"]') as HTMLElement);
      unmount();
      return snapshot;
    };

    it.each([
      ['level colors', { level: 'warning' as const }],
      ['custom bg, auto text, translucent', { color: '#fef08a', opacity: 30 }],
      ['explicit text color, pinned', { color: '#1e1b4b', text_color: '#ff00ff', dismissible: false }],
      ['markdown', { message: '**bold** [docs](https://example.com) `x`' }],
    ])('matches the stack for %s', async (_label, over) => {
      const row = { ...ROW, ...over };
      const expected = realBar(row);
      await renderPage([row]);
      await userEvent.click(screen.getByRole('button', { name: /^Edit announcement/ }));
      expect(snapshotOf(preview())).toEqual(expected);
      // The list row renders the same bar too.
      expect(snapshotOf(rowBar())).toEqual(expected);
    });

    it('holds nothing keyboard-focusable, even with a markdown link in the message', async () => {
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      await userEvent.type(message(), 'see [docs](https://example.com)');
      const wrapper = screen.getByTestId('announcement-preview');
      expect(wrapper.querySelector('a[href]')).not.toBeNull();
      // jsdom does not implement inert: assert what browsers act on, every focusable has an inert ancestor.
      const focusable = [...wrapper.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, [tabindex]')];
      expect(focusable.filter((el) => !el.closest('[inert]'))).toEqual([]);
    });

    it('follows the form live', async () => {
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      await userEvent.type(message(), 'Live');
      fireEvent.change(screen.getByLabelText(/Opacity/), { target: { value: '50' } });
      expect(preview().textContent).toBe('Live');
      expect(preview().style.backgroundColor).toBe('rgba(37, 99, 235, 0.5)');
      await userEvent.click(screen.getByLabelText(/Users can close it/));
      expect(preview().children).toHaveLength(1);
    });

    it('previews the message trimmed, as the server stores it', async () => {
      await renderPage();
      await userEvent.click(screen.getByRole('button', { name: /add announcement/i }));
      // Four leading spaces are an indented code block in markdown.
      fireEvent.change(message(), { target: { value: '    indented  ' } });
      expect(preview().querySelector('code')).toBeNull();
      expect(preview().textContent).toBe('indented');
    });

    it('console copies are no live region and carry no dismiss button', async () => {
      await renderPage([ROW, { ...ROW, id: 'a2', message: 'Second one' }]);
      const table = screen.getByRole('table');
      expect(table.querySelector('[role="status"]')).toBeNull();
      expect(within(table).queryByRole('button', { name: 'Dismiss announcement' })).toBeNull();
      expect(within(table).getByText('Maintenance tonight')).toBeInTheDocument();
      expect(within(table).getByText('Second one')).toBeInTheDocument();
      // The close glyph still shows, hidden from assistive tech.
      expect(rowBar().lastElementChild).toHaveAttribute('aria-hidden', 'true');

      await userEvent.click(screen.getByRole('button', { name: /^Edit announcement: Maintenance/ }));
      expect(preview()).not.toHaveAttribute('role');
      expect(preview().querySelector('button')).toBeNull();
      expect(preview().lastElementChild).toHaveAttribute('aria-hidden', 'true');
    });
  });
});
