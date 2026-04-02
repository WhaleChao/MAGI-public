// MAGI Office - 遊戲主邏輯 (基於 Star Office UI)
// 依賴: layout.js（必須在這個之前載入）

// 檢測瀏覽器是否支援 WebP
let supportsWebP = false;

// 方法 1: 使用 canvas 檢測
function checkWebPSupport() {
  return new Promise((resolve) => {
    const canvas = document.createElement('canvas');
    if (canvas.getContext && canvas.getContext('2d')) {
      resolve(canvas.toDataURL('image/webp').indexOf('data:image/webp') === 0);
    } else {
      resolve(false);
    }
  });
}

// 方法 2: 使用 image 檢測（備用）
function checkWebPSupportFallback() {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(true);
    img.onerror = () => resolve(false);
    img.src = 'data:image/webp;base64,UklGRkoAAABXRUJQVlA4WAoAAAAQAAAAAAAAAAAAQUxQSAwAAAABBxAR/Q9ERP8DAABWUDggGAAAADABAJ0BKgEAAQADADQlpAADcAD++/1QAA==';
  });
}

// 取得檔案副檔名（根據 WebP 支援情況 + 佈局配置的 forcePng）
function getExt(pngFile) {
  // star-working-spritesheet.png 太寬了，WebP 不支援，始終用 PNG
  if (pngFile === 'star-working-spritesheet.png') {
    return '.png';
  }
  // 如果佈局配置裡強制用 PNG，就用 .png
  if (LAYOUT.forcePng && LAYOUT.forcePng[pngFile.replace(/\.(png|webp)$/, '')]) {
    return '.png';
  }
  return supportsWebP ? '.webp' : '.png';
}

const config = {
  type: Phaser.AUTO,
  width: LAYOUT.game.width,
  height: LAYOUT.game.height,
  parent: 'game-container',
  pixelArt: true,
  physics: { default: 'arcade', arcade: { gravity: { y: 0 }, debug: false } },
  scene: { preload: preload, create: create, update: update }
};

let totalAssets = 0;
let loadedAssets = 0;
let loadingProgressBar, loadingProgressContainer, loadingOverlay, loadingText;

// Memo 相關函式（MAGI 版：已停用）
async function loadMemo() {
  // MAGI 版不需要 memo 功能
}

// 更新載入進度
function updateLoadingProgress() {
  loadedAssets++;
  const percent = Math.min(100, Math.round((loadedAssets / totalAssets) * 100));
  if (loadingProgressBar) {
    loadingProgressBar.style.width = percent + '%';
  }
  if (loadingText) {
    loadingText.textContent = `正在載入 MAGI 像素辦公室... ${percent}%`;
  }
}

// 隱藏載入畫面
function hideLoadingOverlay() {
  setTimeout(() => {
    if (loadingOverlay) {
      loadingOverlay.style.transition = 'opacity 0.5s ease';
      loadingOverlay.style.opacity = '0';
      setTimeout(() => {
        loadingOverlay.style.display = 'none';
      }, 500);
    }
  }, 300);
}

const STATES = {
  idle: { name: '待命中', area: 'breakroom' },
  writing: { name: '處理任務', area: 'writing' },
  researching: { name: '搜尋資訊', area: 'researching' },
  executing: { name: '執行任務', area: 'writing' },
  syncing: { name: '同步備份', area: 'writing' },
  error: { name: '發生錯誤', area: 'error' }
};

const BUBBLE_TEXTS = {
  idle: [
    '待命中：隨時可以開工',
    '讓大腦休息一下',
    '今天也要保持高效',
    '等待下一個指令',
    '系統正常，一切就緒',
    '喝杯咖啡再說',
    '先整理一下工作台',
    '隨時待命中'
  ],
  writing: [
    '進入專注模式',
    '正在處理文件',
    '把每一步都記錄下來',
    '快要完成了',
    '認真工作中，請勿打擾',
    '讓系統更完善',
    '穩住，我們能贏',
    '整理案件資料中'
  ],
  researching: [
    '正在搜集證據',
    '讓我把資訊整理好',
    '找到關鍵線索了',
    '研究法條中',
    '查閱相關判決',
    '比對資料庫',
    '深入分析中'
  ],
  executing: [
    '執行中：不要眨眼',
    '任務進行中',
    '啟動自動化流程',
    '一鍵推進',
    '讓結果說話'
  ],
  syncing: [
    '同步中：備份資料',
    '備份是安全感',
    '寫入中...別斷電',
    '雲端對齊中',
    '同步完成前先別動'
  ],
  error: [
    '警報響了：先別慌',
    '偵測到異常',
    '先定位問題根因',
    '錯誤不是敵人，是線索',
    '馬上修復'
  ],
  cat: [
    '喵~',
    '咕嚕咕嚕...',
    '搖搖尾巴',
    '曬太陽最開心',
    '有人來看我啦',
    '我是辦公室的吉祥物',
    '伸個懶腰',
    '今天的罐罐準備好了嗎'
  ]
};

let game, star, sofa, serverroom, areas = {}, currentState = 'idle', pendingDesiredState = null, statusText, lastFetch = 0, lastBlink = 0, lastBubble = 0, targetX = 660, targetY = 170, bubble = null, typewriterText = '', typewriterTarget = '', typewriterIndex = 0, lastTypewriter = 0, syncAnimSprite = null, catBubble = null;
let isMoving = false;
let waypoints = [];
let lastWanderAt = 0;
const FETCH_INTERVAL = 2000;
const BLINK_INTERVAL = 2500;
const BUBBLE_INTERVAL = 8000;
const CAT_BUBBLE_INTERVAL = 18000;
let lastCatBubble = 0;
const TYPEWRITER_DELAY = 50;
let agents = {}; // agentId -> sprite/container
let lastAgentsFetch = 0;
const AGENTS_FETCH_INTERVAL = 2500;

// agent 顏色配置
const AGENT_COLORS = {
  star: 0xffd700,
  npc1: 0x00aaff,
  agent_nika: 0xff69b4,
  melchior: 0x5090e0,    // 藍色
  balthasar: 0x50c050,   // 綠色
  keeper: 0x8090a0,      // 灰色
  watcher: 0x9060c0,     // 紫色
  default: 0x94a3b8
};

// MAGI 節點對應的 guest 精靈編號
const AGENT_GUEST_SPRITE = {
  melchior: 1,
  balthasar: 2,
  keeper: 3,
  watcher: 4
};

// agent 名字顏色
const NAME_TAG_COLORS = {
  approved: 0x22c55e,
  pending: 0xf59e0b,
  rejected: 0xef4444,
  offline: 0x64748b,
  default: 0x1f2937
};

// breakroom / writing / error 區域的 agent 分佈位置（多 agent 時錯開）
const AREA_POSITIONS = {
  breakroom: [
    { x: 500, y: 160 },
    { x: 640, y: 260 },
    { x: 780, y: 180 },
    { x: 440, y: 240 },
    { x: 850, y: 260 },
    { x: 560, y: 300 },
    { x: 720, y: 140 },
    { x: 900, y: 200 }
  ],
  writing: [
    { x: 680, y: 280 },
    { x: 880, y: 340 },
    { x: 560, y: 360 },
    { x: 780, y: 220 },
    { x: 950, y: 280 },
    { x: 620, y: 420 },
    { x: 840, y: 400 },
    { x: 500, y: 300 }
  ],
  error: [
    { x: 120, y: 220 },
    { x: 300, y: 280 },
    { x: 200, y: 160 },
    { x: 400, y: 240 },
    { x: 160, y: 320 },
    { x: 340, y: 180 },
    { x: 260, y: 300 },
    { x: 440, y: 200 }
  ]
};


// 初始化：先檢測 WebP 支援，再啟動遊戲
async function initGame() {
  try {
    supportsWebP = await checkWebPSupport();
  } catch (e) {
    try {
      supportsWebP = await checkWebPSupportFallback();
    } catch (e2) {
      supportsWebP = false;
    }
  }

  console.log('WebP 支援:', supportsWebP);
  new Phaser.Game(config);
}

function preload() {
  loadingOverlay = document.getElementById('loading-overlay');
  loadingProgressBar = document.getElementById('loading-progress-bar');
  loadingText = document.getElementById('loading-text');
  loadingProgressContainer = document.getElementById('loading-progress-container');

  // 從 LAYOUT 讀取總資源數量（避免 magic number）
  totalAssets = LAYOUT.totalAssets || 15;
  loadedAssets = 0;

  this.load.on('filecomplete', () => {
    updateLoadingProgress();
  });

  this.load.on('complete', () => {
    hideLoadingOverlay();
  });

  // === 使用實際檔名，不依賴符號連結 ===
  this.load.image('office_bg', '/static/star-office/office_bg_small.webp');
  this.load.spritesheet('star_idle', '/static/star-office/star-idle-v5.png', { frameWidth: 128, frameHeight: 128 });

  this.load.image('sofa_idle', '/static/star-office/sofa-idle-v3.png');
  this.load.image('sofa_busy', '/static/star-office/sofa-idle-v3.png');

  this.load.spritesheet('plants', '/static/star-office/plants-spritesheet.webp', { frameWidth: 160, frameHeight: 160 });
  this.load.spritesheet('posters', '/static/star-office/posters-spritesheet.webp', { frameWidth: 160, frameHeight: 160 });
  this.load.spritesheet('coffee_machine', '/static/star-office/coffee-machine-v3-grid.webp', { frameWidth: 230, frameHeight: 230 });
  this.load.spritesheet('serverroom', '/static/star-office/serverroom-spritesheet.webp', { frameWidth: 180, frameHeight: 251 });

  this.load.spritesheet('error_bug', '/static/star-office/error-bug-spritesheet-grid.webp', { frameWidth: 180, frameHeight: 180 });
  this.load.spritesheet('cats', '/static/star-office/cats-spritesheet.webp', { frameWidth: 160, frameHeight: 160 });
  this.load.image('desk', '/static/star-office/desk-v3.webp');
  this.load.spritesheet('star_working', '/static/star-office/star-working-spritesheet-grid.webp', { frameWidth: 230, frameHeight: 144 });
  this.load.spritesheet('sync_anim', '/static/star-office/sync-animation-v3-grid.webp', { frameWidth: 256, frameHeight: 256 });
  this.load.image('memo_bg', '/static/star-office/memo-bg.webp');

  // 載入客人精靈（用於 MAGI 節點角色）
  for (let i = 1; i <= 6; i++) {
    this.load.spritesheet('guest_anim_' + i, '/static/star-office/guest_anim_' + i + '.webp', { frameWidth: 32, frameHeight: 32 });
    this.load.image('guest_role_' + i, '/static/star-office/guest_role_' + i + '.png');
  }

  // 辦公桌使用 webp 版本
  this.load.image('desk_v2', '/static/star-office/desk-v3.webp');
  this.load.spritesheet('flowers', '/static/star-office/flowers-bloom-v2.webp', { frameWidth: 65, frameHeight: 65 });
}

function create() {
  game = this;
  this.add.image(640, 360, 'office_bg');

  // === 沙發（來自 LAYOUT）===
  sofa = this.add.sprite(
    LAYOUT.furniture.sofa.x,
    LAYOUT.furniture.sofa.y,
    'sofa_busy'
  ).setOrigin(LAYOUT.furniture.sofa.origin.x, LAYOUT.furniture.sofa.origin.y);
  sofa.setDepth(LAYOUT.furniture.sofa.depth);

  // sofa_busy 現在是靜態圖片，不需要動畫

  areas = LAYOUT.areas;

  this.anims.create({
    key: 'star_idle',
    frames: this.anims.generateFrameNumbers('star_idle', { start: 0, end: 29 }),
    frameRate: 12,
    repeat: -1
  });
  // star_researching 動畫已移除，改用 star_working

  star = game.physics.add.sprite(areas.breakroom.x, areas.breakroom.y, 'star_idle');
  star.setOrigin(0.5);
  star.setScale(1.4);
  star.setAlpha(0.95);
  star.setDepth(20);
  star.setVisible(false);
  star.anims.stop();

  if (game.textures.exists('sofa_busy')) {
    sofa.setTexture('sofa_busy');
  }

  // === 牌匾（來自 LAYOUT）===
  const plaqueX = LAYOUT.plaque.x;
  const plaqueY = LAYOUT.plaque.y;
  const plaqueBg = game.add.rectangle(plaqueX, plaqueY, LAYOUT.plaque.width, LAYOUT.plaque.height, 0x5d4037);
  plaqueBg.setStrokeStyle(3, 0x3e2723);
  const plaqueText = game.add.text(plaqueX, plaqueY, 'MAGI 像素辦公室', {
    fontFamily: 'ArkPixel, monospace',
    fontSize: '18px',
    fill: '#ffd700',
    fontWeight: 'bold',
    stroke: '#000',
    strokeThickness: 2
  }).setOrigin(0.5);
  game.add.text(plaqueX - 190, plaqueY, '\u2b50', { fontFamily: 'ArkPixel, monospace', fontSize: '20px' }).setOrigin(0.5);
  game.add.text(plaqueX + 190, plaqueY, '\u2b50', { fontFamily: 'ArkPixel, monospace', fontSize: '20px' }).setOrigin(0.5);

  // === 植物們（來自 LAYOUT）===
  const plantFrameCount = 16;
  for (let i = 0; i < LAYOUT.furniture.plants.length; i++) {
    const p = LAYOUT.furniture.plants[i];
    const randomPlantFrame = Math.floor(Math.random() * plantFrameCount);
    const plant = game.add.sprite(p.x, p.y, 'plants', randomPlantFrame).setOrigin(0.5);
    plant.setDepth(p.depth);
    plant.setInteractive({ useHandCursor: true });
    window[`plantSprite${i === 0 ? '' : i + 1}`] = plant;
    plant.on('pointerdown', (() => {
      const next = Math.floor(Math.random() * plantFrameCount);
      plant.setFrame(next);
    }));
  }

  // === 海報（來自 LAYOUT）===
  const postersFrameCount = 32;
  const randomPosterFrame = Math.floor(Math.random() * postersFrameCount);
  const poster = game.add.sprite(LAYOUT.furniture.poster.x, LAYOUT.furniture.poster.y, 'posters', randomPosterFrame).setOrigin(0.5);
  poster.setDepth(LAYOUT.furniture.poster.depth);
  poster.setInteractive({ useHandCursor: true });
  window.posterSprite = poster;
  window.posterFrameCount = postersFrameCount;
  poster.on('pointerdown', () => {
    const next = Math.floor(Math.random() * window.posterFrameCount);
    window.posterSprite.setFrame(next);
  });

  // === 小貓已移除，Casper 改用 guest 精靈 ===

  // === 咖啡機（來自 LAYOUT）===
  this.anims.create({
    key: 'coffee_machine',
    frames: this.anims.generateFrameNumbers('coffee_machine', { start: 0, end: 95 }),
    frameRate: 12.5,
    repeat: -1
  });
  const coffeeMachine = this.add.sprite(
    LAYOUT.furniture.coffeeMachine.x,
    LAYOUT.furniture.coffeeMachine.y,
    'coffee_machine'
  ).setOrigin(LAYOUT.furniture.coffeeMachine.origin.x, LAYOUT.furniture.coffeeMachine.origin.y);
  coffeeMachine.setDepth(LAYOUT.furniture.coffeeMachine.depth);
  coffeeMachine.anims.play('coffee_machine', true);

  // === 伺服器區（來自 LAYOUT）===
  this.anims.create({
    key: 'serverroom_on',
    frames: this.anims.generateFrameNumbers('serverroom', { start: 0, end: 39 }),
    frameRate: 6,
    repeat: -1
  });
  serverroom = this.add.sprite(
    LAYOUT.furniture.serverroom.x,
    LAYOUT.furniture.serverroom.y,
    'serverroom',
    0
  ).setOrigin(LAYOUT.furniture.serverroom.origin.x, LAYOUT.furniture.serverroom.origin.y);
  serverroom.setDepth(LAYOUT.furniture.serverroom.depth);
  serverroom.anims.stop();
  serverroom.setFrame(0);

  // === 新辦公桌（來自 LAYOUT，強制透明 PNG）===
  const desk = this.add.image(
    LAYOUT.furniture.desk.x,
    LAYOUT.furniture.desk.y,
    'desk_v2'
  ).setOrigin(LAYOUT.furniture.desk.origin.x, LAYOUT.furniture.desk.origin.y);
  desk.setDepth(LAYOUT.furniture.desk.depth);

  // === 花盆（來自 LAYOUT）===
  const flowerFrameCount = 16;
  const randomFlowerFrame = Math.floor(Math.random() * flowerFrameCount);
  const flower = this.add.sprite(
    LAYOUT.furniture.flower.x,
    LAYOUT.furniture.flower.y,
    'flowers',
    randomFlowerFrame
  ).setOrigin(LAYOUT.furniture.flower.origin.x, LAYOUT.furniture.flower.origin.y);
  flower.setScale(LAYOUT.furniture.flower.scale || 1);
  flower.setDepth(LAYOUT.furniture.flower.depth);
  flower.setInteractive({ useHandCursor: true });
  window.flowerSprite = flower;
  window.flowerFrameCount = flowerFrameCount;
  flower.on('pointerdown', () => {
    const next = Math.floor(Math.random() * window.flowerFrameCount);
    window.flowerSprite.setFrame(next);
  });

  // === Casper 在桌前工作（用 guest 精靈取代 star_working）===
  this.anims.create({
    key: 'error_bug',
    frames: this.anims.generateFrameNumbers('error_bug', { start: 0, end: 95 }),
    frameRate: 12,
    repeat: -1
  });

  // === 錯誤 bug（來自 LAYOUT）===
  const errorBug = this.add.sprite(
    LAYOUT.furniture.errorBug.x,
    LAYOUT.furniture.errorBug.y,
    'error_bug',
    0
  ).setOrigin(LAYOUT.furniture.errorBug.origin.x, LAYOUT.furniture.errorBug.origin.y);
  errorBug.setDepth(LAYOUT.furniture.errorBug.depth);
  errorBug.setVisible(false);
  errorBug.setScale(LAYOUT.furniture.errorBug.scale);
  errorBug.anims.play('error_bug', true);
  window.errorBug = errorBug;
  window.errorBugDir = 1;

  // Casper 用 guest_anim_5 精靈，放大 3 倍坐在桌前
  const casperSpriteKey = 'guest_anim_5';
  const casperAnimKey = 'guest_anim_5_walk';
  const starWorking = this.add.sprite(
    Math.round(LAYOUT.furniture.starWorking.x),
    Math.round(LAYOUT.furniture.starWorking.y),
    casperSpriteKey,
    0
  ).setOrigin(0.5, 0.5);
  starWorking.setVisible(false);
  starWorking.setScale(3.0);
  starWorking.setDepth(LAYOUT.furniture.starWorking.depth);
  window.starWorking = starWorking;
  window.casperAnimKey = casperAnimKey;

  // === 同步動畫（來自 LAYOUT）===
  this.anims.create({
    key: 'sync_anim',
    frames: this.anims.generateFrameNumbers('sync_anim', { start: 1, end: 48 }),
    frameRate: 12,
    repeat: -1
  });
  syncAnimSprite = this.add.sprite(
    LAYOUT.furniture.syncAnim.x,
    LAYOUT.furniture.syncAnim.y,
    'sync_anim',
    0
  ).setOrigin(LAYOUT.furniture.syncAnim.origin.x, LAYOUT.furniture.syncAnim.origin.y);
  syncAnimSprite.setDepth(LAYOUT.furniture.syncAnim.depth);
  syncAnimSprite.anims.stop();
  syncAnimSprite.setFrame(0);

  // 建立 guest 精靈動畫（用於 MAGI 節點）
  for (let i = 1; i <= 6; i++) {
    const key = 'guest_anim_' + i;
    if (game.textures.exists(key)) {
      this.anims.create({
        key: key + '_walk',
        frames: this.anims.generateFrameNumbers(key, { start: 0, end: 7 }),
        frameRate: 8,
        repeat: -1
      });
    }
  }

  window.starSprite = star;

  statusText = document.getElementById('status-text');

  fetchStatus();
  fetchAgents();

  // 可選除錯：僅在顯式開啟 debug 模式時渲染測試用 agent
  let debugAgents = false;
  try {
    if (typeof window !== 'undefined') {
      if (window.STAR_OFFICE_DEBUG_AGENTS === true) {
        debugAgents = true;
      } else if (window.location && window.location.search && typeof URLSearchParams !== 'undefined') {
        const sp = new URLSearchParams(window.location.search);
        if (sp.get('debugAgents') === '1') {
          debugAgents = true;
        }
      }
    }
  } catch (e) {
    debugAgents = false;
  }

  if (debugAgents) {
    const testNika = {
      agentId: 'agent_nika',
      name: '尼卡',
      isMain: false,
      state: 'writing',
      detail: '在畫像素畫...',
      area: 'writing',
      authStatus: 'approved',
      updated_at: new Date().toISOString()
    };
    renderAgent(testNika);

    window.testNikaState = 'writing';
    window.testNikaTimer = setInterval(() => {
      const states = ['idle', 'writing', 'researching', 'executing'];
      const areas = { idle: 'breakroom', writing: 'writing', researching: 'writing', executing: 'writing' };
      window.testNikaState = states[Math.floor(Math.random() * states.length)];
      const testAgent = {
        agentId: 'agent_nika',
        name: '尼卡',
        isMain: false,
        state: window.testNikaState,
        detail: '在畫像素畫...',
        area: areas[window.testNikaState],
        authStatus: 'approved',
        updated_at: new Date().toISOString()
      };
      renderAgent(testAgent);
    }, 5000);
  }
}

function update(time) {
  if (time - lastFetch > FETCH_INTERVAL) { fetchStatus(); lastFetch = time; }
  if (time - lastAgentsFetch > AGENTS_FETCH_INTERVAL) { fetchAgents(); lastAgentsFetch = time; }

  const effectiveStateForServer = pendingDesiredState || currentState;
  if (serverroom) {
    if (effectiveStateForServer === 'idle') {
      if (serverroom.anims.isPlaying) {
        serverroom.anims.stop();
        serverroom.setFrame(0);
      }
    } else {
      if (!serverroom.anims.isPlaying || serverroom.anims.currentAnim?.key !== 'serverroom_on') {
        serverroom.anims.play('serverroom_on', true);
      }
    }
  }

  if (window.errorBug) {
    if (effectiveStateForServer === 'error') {
      window.errorBug.setVisible(true);
      if (!window.errorBug.anims.isPlaying || window.errorBug.anims.currentAnim?.key !== 'error_bug') {
        window.errorBug.anims.play('error_bug', true);
      }
      const leftX = LAYOUT.furniture.errorBug.pingPong.leftX;
      const rightX = LAYOUT.furniture.errorBug.pingPong.rightX;
      const speed = LAYOUT.furniture.errorBug.pingPong.speed;
      const dir = window.errorBugDir || 1;
      window.errorBug.x += speed * dir;
      window.errorBug.y = LAYOUT.furniture.errorBug.y;
      if (window.errorBug.x >= rightX) {
        window.errorBug.x = rightX;
        window.errorBugDir = -1;
      } else if (window.errorBug.x <= leftX) {
        window.errorBug.x = leftX;
        window.errorBugDir = 1;
      }
    } else {
      window.errorBug.setVisible(false);
      window.errorBug.anims.stop();
    }
  }

  if (syncAnimSprite) {
    if (effectiveStateForServer === 'syncing') {
      if (!syncAnimSprite.anims.isPlaying || syncAnimSprite.anims.currentAnim?.key !== 'sync_anim') {
        syncAnimSprite.anims.play('sync_anim', true);
      }
    } else {
      if (syncAnimSprite.anims.isPlaying) syncAnimSprite.anims.stop();
      syncAnimSprite.setFrame(0);
    }
  }

  if (time - lastBubble > BUBBLE_INTERVAL) {
    showBubble();
    lastBubble = time;
  }
  // cat bubble 已移除

  if (typewriterIndex < typewriterTarget.length && time - lastTypewriter > TYPEWRITER_DELAY) {
    typewriterText += typewriterTarget[typewriterIndex];
    statusText.textContent = typewriterText;
    typewriterIndex++;
    lastTypewriter = time;
  }

  moveStar(time);
}

function normalizeState(s) {
  if (!s) return 'idle';
  if (s === 'working') return 'writing';
  if (s === 'run' || s === 'running') return 'executing';
  if (s === 'sync') return 'syncing';
  if (s === 'research') return 'researching';
  return s;
}

function fetchStatus() {
  fetch('/dashboard/pixel/api/status')
    .then(response => response.json())
    .then(data => {
      const nextState = normalizeState(data.state);
      const stateInfo = STATES[nextState] || STATES.idle;
      const changed = (pendingDesiredState === null) && (nextState !== currentState);
      const nextLine = '[' + stateInfo.name + '] ' + (data.detail || '...');
      if (changed) {
        typewriterTarget = nextLine;
        typewriterText = '';
        typewriterIndex = 0;

        pendingDesiredState = null;
        currentState = nextState;

        if (nextState === 'idle') {
          if (game.textures.exists('sofa_busy')) {
            sofa.setTexture('sofa_busy');
            sofa.setTexture('sofa_busy');
          }
          star.setVisible(false);
          star.anims.stop();
          if (window.starWorking) {
            window.starWorking.setVisible(false);
            window.starWorking.anims.stop();
          }
        } else if (nextState === 'error') {
          if (sofa.anims && sofa.anims.isPlaying) sofa.anims.stop();
          sofa.setTexture('sofa_idle');
          star.setVisible(false);
          star.anims.stop();
          if (window.starWorking) {
            window.starWorking.setVisible(false);
            window.starWorking.anims.stop();
          }
        } else if (nextState === 'syncing') {
          if (sofa.anims && sofa.anims.isPlaying) sofa.anims.stop();
          sofa.setTexture('sofa_idle');
          star.setVisible(false);
          star.anims.stop();
          if (window.starWorking) {
            window.starWorking.setVisible(false);
            window.starWorking.anims.stop();
          }
        } else {
          if (sofa.anims && sofa.anims.isPlaying) sofa.anims.stop();
          sofa.setTexture('sofa_idle');
          star.setVisible(false);
          star.anims.stop();
          if (window.starWorking) {
            window.starWorking.setVisible(true);
            window.starWorking.anims.play(window.casperAnimKey, true);
          }
        }

        if (serverroom) {
          if (nextState === 'idle') {
            serverroom.anims.stop();
            serverroom.setFrame(0);
          } else {
            serverroom.anims.play('serverroom_on', true);
          }
        }

        if (syncAnimSprite) {
          if (nextState === 'syncing') {
            if (!syncAnimSprite.anims.isPlaying || syncAnimSprite.anims.currentAnim?.key !== 'sync_anim') {
              syncAnimSprite.anims.play('sync_anim', true);
            }
          } else {
            if (syncAnimSprite.anims.isPlaying) syncAnimSprite.anims.stop();
            syncAnimSprite.setFrame(0);
          }
        }
      } else {
        if (!typewriterTarget || typewriterTarget !== nextLine) {
          typewriterTarget = nextLine;
          typewriterText = '';
          typewriterIndex = 0;
        }
      }
    })
    .catch(error => {
      typewriterTarget = '連線失敗，正在重試...';
      typewriterText = '';
      typewriterIndex = 0;
    });
}

function moveStar(time) {
  const effectiveState = pendingDesiredState || currentState;

  // idle 狀態：Star 隱藏在沙發中，不需要移動
  if (effectiveState === 'idle' && pendingDesiredState === null) {
    isMoving = false;
    return;
  }

  const dx = targetX - star.x;
  const dy = targetY - star.y;
  const dist = Math.sqrt(dx * dx + dy * dy);
  const speed = 1.4;
  const wobble = Math.sin(time / 200) * 0.8;

  if (dist > 3) {
    star.x += (dx / dist) * speed;
    star.y += (dy / dist) * speed;
    star.setY(star.y + wobble);
    isMoving = true;
  } else {
    if (waypoints && waypoints.length > 0) {
      waypoints.shift();
      if (waypoints.length > 0) {
        targetX = waypoints[0].x;
        targetY = waypoints[0].y;
        isMoving = true;
      } else {
        if (pendingDesiredState !== null) {
          isMoving = false;
          currentState = pendingDesiredState;
          pendingDesiredState = null;

          if (currentState === 'idle') {
            star.setVisible(false);
            star.anims.stop();
            if (window.starWorking) {
              window.starWorking.setVisible(false);
              window.starWorking.anims.stop();
            }
          } else {
            star.setVisible(false);
            star.anims.stop();
            if (window.starWorking) {
              window.starWorking.setVisible(true);
              window.starWorking.anims.play(window.casperAnimKey, true);
            }
          }
        }
      }
    } else {
      if (pendingDesiredState !== null) {
        isMoving = false;
        currentState = pendingDesiredState;
        pendingDesiredState = null;

        if (currentState === 'idle') {
          star.setVisible(false);
          star.anims.stop();
          if (window.starWorking) {
            window.starWorking.setVisible(false);
            window.starWorking.anims.stop();
          }
          if (game.textures.exists('sofa_busy')) {
            sofa.setTexture('sofa_busy');
            sofa.setTexture('sofa_busy');
          }
        } else {
          star.setVisible(false);
          star.anims.stop();
          if (window.starWorking) {
            window.starWorking.setVisible(true);
            window.starWorking.anims.play(window.casperAnimKey, true);
          }
          if (sofa.anims && sofa.anims.isPlaying) sofa.anims.stop();
          sofa.setTexture('sofa_idle');
        }
      }
    }
  }
}

function showBubble() {
  if (bubble) { bubble.destroy(); bubble = null; }
  const texts = BUBBLE_TEXTS[currentState] || BUBBLE_TEXTS.idle;
  if (currentState === 'idle') return;

  let anchorX = star.x;
  let anchorY = star.y;
  if (currentState === 'syncing' && syncAnimSprite && syncAnimSprite.visible) {
    anchorX = syncAnimSprite.x;
    anchorY = syncAnimSprite.y;
  } else if (currentState === 'error' && window.errorBug && window.errorBug.visible) {
    anchorX = window.errorBug.x;
    anchorY = window.errorBug.y;
  } else if (!star.visible && window.starWorking && window.starWorking.visible) {
    anchorX = window.starWorking.x;
    anchorY = window.starWorking.y;
  }

  const text = texts[Math.floor(Math.random() * texts.length)];
  const bubbleY = anchorY - 70;
  const bg = game.add.rectangle(anchorX, bubbleY, text.length * 10 + 20, 28, 0xffffff, 0.95);
  bg.setStrokeStyle(2, 0x000000);
  const txt = game.add.text(anchorX, bubbleY, text, { fontFamily: 'ArkPixel, monospace', fontSize: '12px', fill: '#000', align: 'center' }).setOrigin(0.5);
  bubble = game.add.container(0, 0, [bg, txt]);
  bubble.setDepth(1200);
  setTimeout(() => { if (bubble) { bubble.destroy(); bubble = null; } }, 3000);
}

// showCatBubble 已移除（貓已替換為 Casper 精靈）

function fetchAgents() {
  fetch('/dashboard/pixel/api/agents?t=' + Date.now(), { cache: 'no-store' })
    .then(response => response.json())
    .then(data => {
      if (!Array.isArray(data)) return;
      // 重置位置計數器
      // 按區域分配不同位置索引，避免重疊
      const areaSlots = { breakroom: 0, writing: 0, error: 0 };
      for (let agent of data) {
        const area = agent.area || 'breakroom';
        agent._slotIndex = areaSlots[area] || 0;
        areaSlots[area] = (areaSlots[area] || 0) + 1;
        renderAgent(agent);
      }
      // 移除不再存在的 agent
      const currentIds = new Set(data.map(a => a.agentId));
      for (let id in agents) {
        if (!currentIds.has(id)) {
          if (agents[id]) {
            agents[id].destroy();
            delete agents[id];
          }
        }
      }
    })
    .catch(error => {
      console.error('拉取 agents 失敗:', error);
    });
}

function getAreaPosition(area, slotIndex) {
  const positions = AREA_POSITIONS[area] || AREA_POSITIONS.breakroom;
  const idx = (slotIndex || 0) % positions.length;
  return positions[idx];
}

function renderAgent(agent) {
  const agentId = agent.agentId;
  const name = agent.name || 'Agent';
  const area = agent.area || 'breakroom';
  const authStatus = agent.authStatus || 'pending';
  const isMain = !!agent.isMain;

  // 取得這個 agent 在區域裡的位置
  const pos = getAreaPosition(area, agent._slotIndex || 0);
  const baseX = pos.x;
  const baseY = pos.y;

  // 顏色
  const bodyColor = AGENT_COLORS[agentId] || AGENT_COLORS.default;
  const nameColor = NAME_TAG_COLORS[authStatus] || NAME_TAG_COLORS.default;

  // 透明度（離線/待批准/拒絕時變半透明）
  let alpha = 1;
  if (authStatus === 'pending') alpha = 0.7;
  if (authStatus === 'rejected') alpha = 0.4;
  if (authStatus === 'offline') alpha = 0.5;

  if (!agents[agentId]) {
    // 新建 agent
    const container = game.add.container(baseX, baseY);
    container.setDepth(1200 + (isMain ? 100 : 0));

    // 像素小人：MAGI 節點使用 guest 精靈，其餘用星星圖示
    let starIcon;
    const guestIdx = AGENT_GUEST_SPRITE[agentId];
    if (guestIdx && game.textures.exists('guest_anim_' + guestIdx)) {
      starIcon = game.add.sprite(0, 0, 'guest_anim_' + guestIdx, 0).setOrigin(0.5);
      starIcon.setScale(3.0);
      const animKey = 'guest_anim_' + guestIdx + '_walk';
      if (game.anims.exists(animKey)) {
        starIcon.anims.play(animKey, true);
      }
    } else {
      starIcon = game.add.text(0, 0, '\u2b50', {
        fontFamily: 'ArkPixel, monospace',
        fontSize: '32px'
      }).setOrigin(0.5);
    }
    starIcon.name = 'starIcon';

    // 名字標籤（漂浮）
    const nameTag = game.add.text(0, -56, name, {
      fontFamily: 'ArkPixel, monospace',
      fontSize: '14px',
      fill: '#' + nameColor.toString(16).padStart(6, '0'),
      stroke: '#000',
      strokeThickness: 3,
      backgroundColor: 'rgba(255,255,255,0.95)'
    }).setOrigin(0.5);
    nameTag.name = 'nameTag';

    // 狀態小點（綠色/黃色/紅色）
    let dotColor = 0x64748b;
    if (authStatus === 'approved') dotColor = 0x22c55e;
    if (authStatus === 'pending') dotColor = 0xf59e0b;
    if (authStatus === 'rejected') dotColor = 0xef4444;
    if (authStatus === 'offline') dotColor = 0x94a3b8;
    const statusDot = game.add.circle(36, -36, 6, dotColor, alpha);
    statusDot.setStrokeStyle(2, 0x000000, alpha);
    statusDot.name = 'statusDot';

    container.add([starIcon, statusDot, nameTag]);
    agents[agentId] = container;
  } else {
    // 更新 agent
    const container = agents[agentId];
    container.setPosition(baseX, baseY);
    container.setAlpha(alpha);
    container.setDepth(1200 + (isMain ? 100 : 0));

    // 更新名字和顏色（如果變化）
    const nameTag = container.getAt(2);
    if (nameTag && nameTag.name === 'nameTag') {
      nameTag.setText(name);
      nameTag.setFill('#' + (NAME_TAG_COLORS[authStatus] || NAME_TAG_COLORS.default).toString(16).padStart(6, '0'));
    }
    // 更新狀態點顏色
    const statusDot = container.getAt(1);
    if (statusDot && statusDot.name === 'statusDot') {
      let dotColor = 0x64748b;
      if (authStatus === 'approved') dotColor = 0x22c55e;
      if (authStatus === 'pending') dotColor = 0xf59e0b;
      if (authStatus === 'rejected') dotColor = 0xef4444;
      if (authStatus === 'offline') dotColor = 0x94a3b8;
      statusDot.fillColor = dotColor;
    }
  }
}

// 啟動遊戲
initGame();
