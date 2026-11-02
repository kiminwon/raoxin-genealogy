// 请求封装：统一 baseUrl、token、错误码处理
//
// 后端所有响应都是 { code, msg, data } 形状，code === 0 表示成功。
// 错误码对照见 docs/02-接口定义.md 第 0.3 节。

const env = require('./env');
const session = require('./session');

const app = () => getApp();

function request(path, { method = 'GET', data = {}, showLoading = false, silent = false } = {}) {
  const g = app();
  // 正常路径上 app.js 已把 env.API_BASE 写进 globalData。
  // 这里兜底是因为「App 还没跑完 onLaunch 就有请求发出」确实会发生
  // （比如某个页面 onLoad 里直接发请求），此时 globalData 还是空的。
  const baseUrl = (g && g.globalData.baseUrl) || env.API_BASE;
  // 必须走 session.load()：它会把"别的后端 / 别的身份来源"留下的 token 丢掉，
  // 直接读 Storage 会让切换环境后仍然带着旧身份请求。
  const token = (g && g.globalData.token) || session.load() || '';

  if (showLoading) {
    wx.showLoading({ title: '加载中', mask: true });
  }

  return new Promise((resolve, reject) => {
    wx.request({
      url: baseUrl + path,
      method,
      data,
      timeout: 15000,
      header: {
        'content-type': 'application/json',
        ...(token ? { Authorization: 'Bearer ' + token } : {}),
      },
      success(res) {
        const body = res.data || {};

        if (body.code === 0) {
          resolve(body.data);
          return;
        }

        // 40100：token 失效，清掉后让调用方重试
        if (body.code === 40100) {
          g && (g.globalData.token = '');
          session.clear();
        }

        const err = new Error(body.msg || '请求失败');
        err.code = body.code;
        err.data = body.data;

        if (!silent) {
          wx.showToast({ title: err.message, icon: 'none', duration: 2500 });
        }
        reject(err);
      },
      fail(e) {
        const err = new Error('网络异常，请检查后端是否已启动');
        err.code = -1;
        err.raw = e;
        if (!silent) {
          wx.showToast({ title: err.message, icon: 'none', duration: 3000 });
        }
        reject(err);
      },
      complete() {
        if (showLoading) wx.hideLoading();
      },
    });
  });
}

const get = (path, data, opt) => request(path, { method: 'GET', data, ...opt });
const post = (path, data, opt) => request(path, { method: 'POST', data, ...opt });
const patch = (path, data, opt) => request(path, { method: 'PATCH', data, ...opt });

module.exports = { request, get, post, patch };
