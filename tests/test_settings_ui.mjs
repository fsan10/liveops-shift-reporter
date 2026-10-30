import assert from 'node:assert/strict';
import test from 'node:test';
import '../liveops/static/settings-form.js';

const { contract, changedValues } = globalThis.liveopsSettingsForm;

test('changing the business group against an older backend does not submit new Bitable keys', () => {
  const saved = { feishu_chat_id: 'anchor', feishu_hourly_chat_id: 'business-old', hourly_message_template: 'custom message' };
  const schema = contract({ settings: saved, secret_keys: ['feishu_app_secret'] });
  const patch = changedValues({ ...saved, feishu_hourly_chat_id: 'business-new', bitable_parent_field: '',
    bitable_hourly_view_id: '', feishu_app_secret: '' }, saved, schema);
  assert.deepEqual(patch, { values: { feishu_hourly_chat_id: 'business-new' }, unsupported: [] });
  assert.equal(schema.supported.has('bitable_parent_field'), false);
});

test('a current backend can save the parent field and hierarchy view together', () => {
  const saved = { bitable_parent_field: '父记录', bitable_hourly_view_id: '', feishu_hourly_chat_id: 'business' };
  const patch = changedValues({ ...saved, bitable_parent_field: '所属主播', bitable_hourly_view_id: 'view-1' },
    saved, contract({ settings: saved }));
  assert.deepEqual(patch.values, { bitable_parent_field: '所属主播', bitable_hourly_view_id: 'view-1' });
  assert.deepEqual(patch.unsupported, []);
});

test('unmodified fields produce no write and cannot overwrite another setting', () => {
  const saved = { feishu_hourly_chat_id: 'business', feishu_chat_id: 'anchor', shop_name: 'shop' };
  assert.deepEqual(changedValues({ ...saved }, saved, contract({ settings: saved })), { values: {}, unsupported: [] });
});

test('blank secrets are preserved while an explicitly entered replacement is included', () => {
  const saved = { feishu_app_id: 'app', feishu_app_secret_configured: true, feishu_app_secret_masked: 'ma******ed' };
  const schema = contract({ settings: saved, secret_keys: ['feishu_app_secret'] });
  assert.deepEqual(changedValues({ feishu_app_id: 'app', feishu_app_secret: '  ' }, saved, schema).values, {});
  assert.deepEqual(changedValues({ feishu_app_secret: 'new-test-secret' }, saved, schema).values,
    { feishu_app_secret: 'new-test-secret' });
});

test('an unsupported edited field is identified instead of being silently treated as saved', () => {
  const saved = { feishu_hourly_chat_id: 'business' };
  const patch = changedValues({ feishu_hourly_chat_id: 'new-business', bitable_parent_field: '父记录' },
    saved, contract({ settings: saved }));
  assert.deepEqual(patch.unsupported, ['bitable_parent_field']);
  assert.deepEqual(patch.values, { feishu_hourly_chat_id: 'new-business' });
});

test('turning a supported switch off is retained as a real change', () => {
  const saved = { hourly_screenshot_enabled: 'true', anchor_screenshot_enabled: 'false' };
  assert.deepEqual(changedValues({ hourly_screenshot_enabled: 'false', anchor_screenshot_enabled: 'false' },
    saved, contract({ settings: saved })).values, { hourly_screenshot_enabled: 'false' });
});
