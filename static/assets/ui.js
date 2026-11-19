(() => {
  const words={ru:{brand:'Цифровой помощник школы',nav:'Рабочее пространство',private:'Пространство школы',privateHint:'Посещаемость, классы и связь с руководителями.',timezone:'Время школы',skip:'К содержимому',menu:'Открыть меню',close:'Закрыть меню',dark:'Включить тёмную тему',light:'Включить светлую тему',logout:'Выйти',workspace:'Обзор',loginTitle:'Добро пожаловать',loginSubtitle:'Войдите, чтобы открыть пространство вашей школы.',password:'Пароль сотрудника',passwordPlaceholder:'Введите пароль',signIn:'Войти в систему',loginNote:'Доступ для сотрудников школы',storyTitle:'Школьный день.\nВсё под контролем.',storyLead:'Присутствие учеников, порядок в классах и связь с руководителями — в одном пространстве.',storyOne:'Посещаемость без лишних шагов',storyOneSub:'Отметки и время прихода',storyTwo:'Нужные сообщения вовремя',storyTwoSub:'Уведомления в Telegram',storyFoot:'Создано для повседневной жизни школы',loginError:'Неверный пароль'},kk:{brand:'Мектептің цифрлық көмекшісі',nav:'Жұмыс кеңістігі',private:'Мектеп кеңістігі',privateHint:'Қатысу, сыныптар және жетекшілермен байланыс.',timezone:'Мектеп уақыты',skip:'Мазмұнға өту',menu:'Мәзірді ашу',close:'Мәзірді жабу',dark:'Қараңғы тақырыпты қосу',light:'Жарық тақырыпты қосу',logout:'Шығу',workspace:'Шолу',loginTitle:'Қош келдіңіз',loginSubtitle:'Мектебіңіздің жұмыс кеңістігіне кіріңіз.',password:'Қызметкер құпиясөзі',passwordPlaceholder:'Құпиясөзді енгізіңіз',signIn:'Жүйеге кіру',loginNote:'Мектеп қызметкерлеріне арналған',storyTitle:'Мектеп күні.\nБәрі назарда.',storyLead:'Оқушылардың қатысуы, сынып тазалығы және жетекшілермен байланыс — бір кеңістікте.',storyOne:'Қатысуды бақылау оңай',storyOneSub:'Келу белгілері мен уақыты',storyTwo:'Қажетті хабарлама дер кезінде',storyTwoSub:'Telegram хабарламалары',storyFoot:'Мектептің күнделікті өміріне арналған',loginError:'Құпиясөз қате'}};
  let theme='light';
  try{theme=localStorage.getItem('school-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');}catch(e){}
  const themeButton=document.getElementById('theme-toggle');
  function lang(){return document.documentElement.lang==='ru'?'ru':'kk';}
  function refresh(){
    const t=words[lang()];
    document.querySelectorAll('[data-ui]').forEach(n=>n.textContent=t[n.dataset.ui]||'');
    document.querySelectorAll('[data-ui-label]').forEach(n=>{n.setAttribute('aria-label',t[n.dataset.uiLabel]);n.title=t[n.dataset.uiLabel];});
    document.querySelectorAll('[data-ui-placeholder]').forEach(n=>n.placeholder=t[n.dataset.uiPlaceholder]);
    if(themeButton){themeButton.title=t[theme==='dark'?'light':'dark'];themeButton.setAttribute('aria-label',themeButton.title);themeButton.setAttribute('aria-pressed',String(theme==='dark'));}
    if(document.body.classList.contains('login-page'))document.title='School Face · '+t.signIn;
    const loginLang=document.getElementById('login-lang');if(loginLang)loginLang.textContent=lang()==='ru'?'ҚАЗ':'РУС';
  }
  function applyTheme(){document.documentElement.dataset.theme=theme;refresh();}
  themeButton?.addEventListener('click',()=>{theme=theme==='dark'?'light':'dark';try{localStorage.setItem('school-theme',theme);}catch(e){}applyTheme();});
  const menu=document.getElementById('menu-toggle'),sidebar=document.getElementById('sidebar');
  function close(){document.body.classList.remove('menu-open');menu?.setAttribute('aria-expanded','false');if(sidebar&&innerWidth<=800)sidebar.inert=true;}
  menu?.addEventListener('click',()=>{const open=document.body.classList.toggle('menu-open');menu.setAttribute('aria-expanded',String(open));if(sidebar)sidebar.inert=!open;if(open)sidebar?.querySelector('a')?.focus();});
  document.getElementById('sidebar-backdrop')?.addEventListener('click',close);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('menu-open')){close();menu?.focus();}if(e.key==='Tab'&&document.body.classList.contains('menu-open')&&sidebar){const nodes=[...sidebar.querySelectorAll('a,button')];const first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}});
  function resize(){if(innerWidth>800){close();if(sidebar)sidebar.inert=false;}else if(sidebar&&!document.body.classList.contains('menu-open'))sidebar.inert=true;}
  addEventListener('resize',resize);resize();
  document.getElementById('login-lang')?.addEventListener('click',()=>{const next=lang()==='ru'?'kk':'ru';document.documentElement.lang=next;try{localStorage.setItem('lang',next);}catch(e){}refresh();});
  if(document.body.classList.contains('login-page')){try{document.documentElement.lang=localStorage.getItem('lang')==='ru'?'ru':'kk';}catch(e){}}
  new MutationObserver(refresh).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  applyTheme();
})();
