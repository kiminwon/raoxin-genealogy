// token 的存取 —— **唯一入口**，别处不要直接碰 Storage。
//
// 为什么不能让各处自己 wx.getStorageSync('token')：
//   身份来源换过一次（固定开发身份 → 真微信登录），手机里那条旧 token
//   仍然有效（JWT 7 天），后端解出来还是原来那行 app_user —— 于是你明明改了
//   代码，整个体验版却还顶着老身份跑（"所有人都变成陈雨欣"就是这么来的）。
//
//   所以 token 必须和「它是哪个后端、哪种身份来源签发的」绑在一起存。
//   标记对不上就当场丢掉，宁可他重新登录一次，也不能带着错身份继续用。
//
// 标记里为什么要带 BACKEND：同一个微信号连本机后端和连云端是两套数据
// （本机库和云库不是一份），token 混用会拿到对面库里的 user id，
// 要么查不到人、要么认到别的人身上。
const { BACKEND, LOGIN_MODE } = require('./env');

const TOKEN_KEY = 'token';
const TAG_KEY = 'tokenTag';

/** 当前环境标记。改这两个维度（换后端 / 换身份来源）任一项，旧 token 自动作废。 */
function tag() {
  return BACKEND + ':' + LOGIN_MODE;
}

/** 读 token。标记不符或读不到都返回 ''。 */
function load() {
  try {
    if (wx.getStorageSync(TAG_KEY) !== tag()) {
      clear();
      return '';
    }
    return wx.getStorageSync(TOKEN_KEY) || '';
  } catch (e) {
    return '';
  }
}

/** 存 token。写不进去也不影响本次会话（内存里有）。 */
function save(token) {
  if (!token) return;
  try {
    wx.setStorageSync(TOKEN_KEY, token);
    wx.setStorageSync(TAG_KEY, tag());
  } catch (e) {
    // Storage 满了之类。不值得打断登录，下一次启动重新登录即可。
  }
}

function clear() {
  try {
    wx.removeStorageSync(TOKEN_KEY);
    wx.removeStorageSync(TAG_KEY);
  } catch (e) {
    // 忽略
  }
}

module.exports = { load, save, clear, tag };
