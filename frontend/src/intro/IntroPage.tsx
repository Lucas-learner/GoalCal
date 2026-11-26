import { useEffect, useState } from 'react';
import { BrandMark } from '../components/BrandMark';
import { useI18n } from '../i18n';
import {
  AssistantDemo,
  CalendarSyncDemo,
  HabitDemo,
  HeroProduct,
  ReminderDemo,
  StoryVisual,
  type StoryIndex,
} from './ProductVisuals';

function useReducedMotion() {
  const [reduced, setReduced] = useState(() => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(query.matches);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return reduced;
}

function Brand() {
  return <span className="intro-brand"><BrandMark /><span>GoalCal</span></span>;
}

export function IntroPage() {
  const { lang, t, setPreference } = useI18n();
  const reducedMotion = useReducedMotion();
  const [activeStory, setActiveStory] = useState<StoryIndex>(0);

  const stories = [
    {
      number: '01',
      label: t('澄清目标', 'Clarify'),
      description: t('先从一句话开始。信息不够时，GoalCal 会先追问，再生成草稿。', 'Start with one sentence. GoalCal asks for missing details before generating a draft.'),
    },
    {
      number: '02',
      label: t('修改草稿', 'Revise'),
      description: t('检查、编辑或重新生成，合适再保存。', 'Review, edit, or regenerate. Save when you’re ready.'),
    },
    {
      number: '03',
      label: t('安排时间', 'Schedule'),
      description: t('选择推荐时段，或自定义时间。', 'Pick a suggested slot or set a custom time.'),
    },
    {
      number: '04',
      label: t('当天执行', 'Start'),
      description: t('打开「今天」，开始训练并记录进度。', 'Open Today to start training and log progress.'),
    },
    {
      number: '05',
      label: t('批量安排', 'Plan week'),
      description: t('把多个目标的步骤排进一周，检查后一次采纳。', 'Schedule steps from multiple goals, review the week, and accept them together.'),
    },
  ];

  useEffect(() => {
    document.title = lang === 'zh' ? 'GoalCal — 把长期目标，变成今天能做的一步' : 'GoalCal — Turn a long-term goal into something you can do today';
    document.querySelector('meta[name="description"]')?.setAttribute('content', lang === 'zh'
      ? 'GoalCal 帮你把长期目标拆成具体行动，在日历里安排时间，并在计划有变时保留进度。'
      : 'GoalCal turns long-term goals into concrete actions, finds time on your calendar, and helps you adjust what’s ahead without losing progress.');
  }, [lang]);

  useEffect(() => {
    const elements = Array.from(document.querySelectorAll<HTMLElement>('[data-reveal]'));
    if (reducedMotion || typeof IntersectionObserver === 'undefined') {
      elements.forEach((element) => element.classList.add('is-visible'));
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [reducedMotion]);

  useEffect(() => {
    const chapters = Array.from(document.querySelectorAll<HTMLElement>('[data-story-index]'));
    let frame = 0;
    const update = () => {
      frame = 0;
      const center = window.innerHeight * 0.42;
      const nearest = chapters.reduce<{ index: StoryIndex; distance: number }>((best, chapter) => {
        const rect = chapter.getBoundingClientRect();
        const distance = rect.top <= center && rect.bottom >= center
          ? 0
          : Math.min(Math.abs(rect.top - center), Math.abs(rect.bottom - center));
        return distance < best.distance
          ? { index: Number(chapter.dataset.storyIndex) as StoryIndex, distance }
          : best;
      }, { index: 0 as StoryIndex, distance: Number.POSITIVE_INFINITY });
      setActiveStory(nearest.index);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    return () => {
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  const goToStory = (index: StoryIndex) => {
    setActiveStory(index);
    document.getElementById(`story-${index}`)?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
  };

  return (
    <div className="intro-page" id="top">
      <a className="intro-skip-link" href="#main">{t('跳到主要内容', 'Skip to content')}</a>
      <header className="intro-nav">
        <div className="intro-nav-inner">
          <a href="#top" aria-label={t('GoalCal 首页', 'GoalCal home')}><Brand /></a>
          <div className="intro-nav-actions">
            <div className="intro-language" role="group" aria-label={t('语言', 'Language')}>
              <button type="button" className={lang === 'zh' ? 'active' : ''} aria-pressed={lang === 'zh'} onClick={() => setPreference('zh')}>中</button>
              <button type="button" className={lang === 'en' ? 'active' : ''} aria-pressed={lang === 'en'} onClick={() => setPreference('en')}>EN</button>
            </div>
            <a className="intro-login" href="/_goalcal/login?next=/app/">{t('登录', 'Log in')}</a>
            <a className="intro-nav-cta" href="/_goalcal/register?next=/app/">{t('注册', 'Sign up')}</a>
          </div>
        </div>
      </header>

      <main id="main">
        <section className="intro-hero" aria-labelledby="hero-title">
          <div className="hero-grid-pattern" aria-hidden="true" />
          <div className="hero-orbit orbit-one" aria-hidden="true" />
          <div className="hero-orbit orbit-two" aria-hidden="true" />
          <div className="intro-container hero-content">
            <div className="hero-eyebrow"><span className="eyebrow-pulse" />{t('为目标找到节奏', 'A RHYTHM FOR YOUR AMBITIONS')}<span>Goal → Cal</span></div>
            <h1 id="hero-title">{t('把长期目标，', 'Turn a long-term goal')}<br /><span>{t('变成今天', 'into something ')}<br className="hero-title-mobile-break" />{t('能做的一步。', 'you can do today.')}</span></h1>
            <p className="hero-subtitle">{t('说出目标，拆成行动，在日历里安排时间。计划有变时，保留进度，继续向前。', 'Turn your goal into actions and make time for them. When plans change, keep your progress and adjust what’s next.')}</p>
            <div className="hero-actions">
              <a className="intro-button primary" href="/_goalcal/register?next=/app/">{t('开始使用', 'Get started')}<span aria-hidden="true">↗</span></a>
              <a className="intro-button secondary" href="#how-it-works">{t('看看它如何工作', 'See how it works')}<span aria-hidden="true">↓</span></a>
            </div>
            <div className="hero-value-line"><span>{t('澄清目标', 'Clarify')}</span><i /><span>{t('灵活计划', 'Plan')}</span><i /><span>{t('专注今天', 'Focus')}</span><i /><span>{t('随时调整', 'Adapt')}</span></div>
            <HeroProduct />
          </div>
        </section>

        <section className="story-section" id="how-it-works" aria-labelledby="story-heading">
          <div className="intro-container">
            <div className="section-heading story-section-heading" data-reveal>
              <div><span className="section-kicker">{t('如何工作', 'HOW IT WORKS')}</span><h2 id="story-heading">{t('不是管理每一分钟。', 'It’s not about managing every minute.')}<br /><span>{t('是让每一步，都有着落。', 'It’s about giving every step a place.')}</span></h2></div>
              <p>{t('以「8 周内完成人生第一个 10 公里」为例，从一句目标到第一次训练。', 'From “my first 10K in eight weeks” to the first training session.')}</p>
            </div>
            <div className={`story-layout story-active-${activeStory}`}>
              <div className="story-copy-column">
                {stories.map((story, index) => <article className={`story-chapter${activeStory === index ? ' active' : ''}`} id={`story-${index}`} data-story-index={index} key={story.number}>
                  <h3><span className="story-chapter-number">{story.number}</span>{story.label}</h3>
                  <p>{story.description}</p>
                  <div className="story-mobile-visual" aria-hidden="true"><StoryVisual story={index as StoryIndex} /></div>
                </article>)}
              </div>
              <div className="story-sticky-column">
                <div className="story-sticky-stage">
                  <div className="story-progress" role="group" aria-label={t('产品步骤', 'Product story chapters')}>
                    {stories.map((story, index) => <button type="button" className={activeStory === index ? 'active' : ''} aria-current={activeStory === index ? 'step' : undefined} onClick={() => goToStory(index as StoryIndex)} key={story.number}><span>{story.number}</span>{story.label}</button>)}
                  </div>
                  <div className="story-stage-visual" aria-hidden="true"><StoryVisual story={activeStory} /></div>
                  <div className="story-stage-caption"><span>{stories[activeStory].number} / 05</span><i /><p>{stories[activeStory].label}</p><span>{t('滚动继续', 'SCROLL TO CONTINUE')} ↓</span></div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="features-section" id="features" aria-labelledby="features-heading">
          <div className="intro-container">
            <div className="section-heading features-heading" data-reveal>
              <div><span className="section-kicker">{t('与生活连接', 'CONNECTED TO REAL LIFE')}</span><h2 id="features-heading">{t('生活的变量，\n也是计划的一部分。', 'Real life is part of the plan.')}</h2></div>
              <p>{t('连接已有日历，让目标、习惯和提醒进入真实生活。', 'Bring goals, habits, and reminders into the calendar you already use.')}</p>
            </div>
            <div className="feature-bento">
              <article className="feature-card sync-feature" data-reveal>
                <div className="feature-card-copy"><span className="section-kicker">CALENDAR SYNC</span><h3>{t('你的日历，终于一起工作。', 'Your calendars, finally working together.')}</h3><p>{t('在同一日历查看外部日程和 GoalCal 行动。', 'See external events and GoalCal actions together.')}</p><div className="feature-point-list"><span><b>01</b>{t('专属 GoalCal 子日历', 'A dedicated GoalCal calendar')}</span><span><b>02</b>{t('来源配色与排期避让', 'Source colors and scheduling controls')}</span><span><b>03</b>{t('冲突检测与一键重排', 'Conflict detection and one-click replanning')}</span></div></div>
                <CalendarSyncDemo />
              </article>
              <article className="feature-card habit-feature" data-reveal>
                <div className="feature-card-copy"><span className="section-kicker">FLEXIBLE HABITS</span><h3>{t('习惯有固定，\n也有弹性。', 'Structure for habits. Flexibility for life.')}</h3><p>{t('固定日或每周次数，都能安排、完成与补记。', 'Fixed days or weekly frequency, with scheduling, completion, and backfill.')}</p></div>
                <HabitDemo />
              </article>
              <article className="feature-card assistant-feature" data-reveal>
                <div className="feature-card-copy"><span className="section-kicker">ASK GOALCAL</span><h3>{t('GoalCal 助手。\n从一句话，到下一步。', 'GoalCal Assistant. From one sentence to the next step.')}</h3><p>{t('起草目标与习惯，查找空档，安排或调整日程。', 'Draft goals and habits, find time, and schedule or adjust your calendar.')}</p></div>
                <AssistantDemo />
              </article>
              <article className="feature-card reminder-feature" data-reveal>
                <div className="feature-card-copy"><span className="section-kicker">GENTLE REMINDERS</span><h3>{t('提醒，但不轰炸。', 'A nudge, not a notification storm.')}</h3><p>{t('系统日历到点提醒，邮件发送早晚摘要与每周复盘。支持免打扰和合并发送。', 'Calendar alarms, daily email digests, and weekly reviews—with quiet hours and grouped delivery.')}</p></div>
                <ReminderDemo />
              </article>
            </div>
          </div>
        </section>

        <section className="intro-closing">
          <div className="closing-grid" aria-hidden="true" />
          <div className="intro-container closing-content" data-reveal>
            <span className="section-kicker">YOUR NEXT STEP</span>
            <h2>{t('不是把生活塞满，', 'Don’t fill every moment.')}<br /><span>{t('是让重要的事有位置。', 'Make room for what matters.')}</span></h2>
            <p>{t('带上一个目标开始。', 'Start with one goal.')}</p>
            <div className="hero-actions"><a className="intro-button closing-primary" href="/_goalcal/register?next=/app/">{t('开始规划我的目标', 'Start planning my goal')}<span aria-hidden="true">↗</span></a><a className="closing-login" href="/_goalcal/login?next=/app/">{t('已有账号？登录', 'Already have an account? Log in')} →</a></div>
            <div className="closing-meta"><span><i />{t('桌面与手机 Web', 'Desktop & mobile web')}</span><span>中文 / English</span><span>Apple · Google</span></div>
          </div>
        </section>
      </main>

      <footer className="intro-footer">
        <div className="intro-container footer-inner"><a href="#top"><Brand /></a><p>{t('让计划适应你的生活节奏。', 'Plans that adapt to the rhythm of your life.')}</p><div><a href="#how-it-works">{t('如何工作', 'How it works')}</a><a href="/_goalcal/login?next=/app/">{t('登录', 'Log in')}</a><span>© {new Date().getFullYear()} GoalCal</span></div></div>
      </footer>
    </div>
  );
}
