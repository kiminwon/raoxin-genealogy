// 通用格式化。
//
// 服务端返回的是「数据」，不是「文案」。所有面向用户的措辞都在这里统一，
// 换一种说法只改一个文件，不用翻遍页面。
//
// 与后端 app/services/mask.py 的 cn_number 保持同一套规则：
// 后端算"第十九世"给详情页用，前端算给树上节点的窄标签用（后端 tree 接口不返回这个字段，
// 因为每个节点都带上一个字符串会让全族总览的包大一圈）。

const CN_DIGITS = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九'];

/** 19 -> 十九，24 -> 二十四 */
function cnNumber(n) {
  if (n === null || n === undefined || n === '') return '';
  n = Number(n);
  if (isNaN(n)) return '';
  if (n < 0) return String(n);
  if (n < 10) return CN_DIGITS[n];
  if (n < 20) return '十' + (n % 10 ? CN_DIGITS[n % 10] : '');
  if (n < 100) return CN_DIGITS[Math.floor(n / 10)] + '十' + (n % 10 ? CN_DIGITS[n % 10] : '');
  return String(n);
}

/** 3 -> 第三世（树上窄标签用简写「3世」由调用方决定） */
function genLabel(n) {
  const s = cnNumber(n);
  return s ? `第${s}世` : '';
}

/** 谱系角色 */
const ROLE_TEXT = {
  1: '本族男丁',
  2: '本族女儿',
  3: '媳妇',
  4: '女婿',
  5: '外孙',
};

function roleText(role) {
  return ROLE_TEXT[role] || '';
}

/** 关系的称呼。用于详情页亲属区块的标题。 */
const RELATION_TEXT = {
  father: '父亲',
  mother: '母亲',
  spouse: '配偶',
  child: '子女',
  sibling: '兄弟姐妹',
};

function relationText(r) {
  return RELATION_TEXT[r] || r || '';
}

/** 数字世次 -> "第N世"，兼容后端已给 generationLabel 的情况 */
function genLabelOf(person) {
  if (!person) return '';
  if (person.generationLabel) return person.generationLabel;
  return genLabel(person.generation);
}

/**
 * 日期输出。
 * 后端给的是 { date, precision, display }，display 已经是"1965年3月12日"这种成品，
 * 优先直接用；没有则退回 date。
 */
function dateText(d) {
  if (!d) return '';
  if (typeof d === 'string') return d;
  return d.display || d.date || '';
}

/** 生卒合并成一行：1965年生 / 1940—2011 */
function lifeSpan(person) {
  if (!person) return '';
  const b = dateText(person.birth);
  const d = dateText(person.death);
  const living = person.livingStatus === 1;

  if (living) return b ? `${b}生` : '';
  if (b && d) return `${b.replace(/年.*$/, '')}—${d}`;
  if (b) return `${b}生`;
  if (d) return `卒于${d}`;
  return '';
}

/** 在世状态文案 */
function livingText(status) {
  return { 1: '在世', 2: '已故', 3: '不详' }[status] || '';
}

/**
 * 树上的节点标签：只用姓名的后两字或全名。
 * 脱敏后的 "陈*明" 直接显示——星号本身就是设计的一部分，
 * 它让访客明确知道"这里有人，但你还看不到"，比显示空白更有推动力。
 */
function nodeLabel(node) {
  if (!node) return '';
  return node.displayName || node.name || '未详';
}

/** 头像位用的单字：取姓氏或名字首字 */
function initial(node) {
  const s = nodeLabel(node);
  return s ? s[0] : '？';
}

module.exports = {
  cnNumber,
  genLabel,
  genLabelOf,
  roleText,
  relationText,
  dateText,
  lifeSpan,
  livingText,
  nodeLabel,
  initial,
};
