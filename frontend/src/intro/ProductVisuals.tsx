import { useI18n } from '../i18n';
import heroZh from './assets/hero-zh.webp';
import heroEn from './assets/hero-en.webp';
import storyZh0 from './assets/story-zh-0.webp';
import storyZh1 from './assets/story-zh-1.webp';
import storyZh2 from './assets/story-zh-2.webp';
import storyZh3 from './assets/story-zh-3.webp';
import storyZh4 from './assets/story-zh-4.webp';
import storyEn0 from './assets/story-en-0.webp';
import storyEn1 from './assets/story-en-1.webp';
import storyEn2 from './assets/story-en-2.webp';
import storyEn3 from './assets/story-en-3.webp';
import storyEn4 from './assets/story-en-4.webp';
import syncZh from './assets/sync-zh.webp';
import syncEn from './assets/sync-en.webp';
import featureZhHabit from './assets/feature-zh-habit.webp';
import featureZhAssistant from './assets/feature-zh-assistant.webp';
import featureZhReminders from './assets/feature-zh-reminders.webp';
import featureEnHabit from './assets/feature-en-habit.webp';
import featureEnAssistant from './assets/feature-en-assistant.webp';
import featureEnReminders from './assets/feature-en-reminders.webp';
import featureZhCalendarAlarm from './assets/feature-zh-calendar-alarm.webp';
import featureEnCalendarAlarm from './assets/feature-en-calendar-alarm.webp';

export type StoryIndex = 0 | 1 | 2 | 3 | 4;
type FeatureName = 'calendar' | 'habit' | 'assistant' | 'reminders';

const heroImages = {
  zh: heroZh,
  en: heroEn,
};

const storyImages = {
  zh: [storyZh0, storyZh1, storyZh2, storyZh3, storyZh4],
  en: [storyEn0, storyEn1, storyEn2, storyEn3, storyEn4],
};

const featureImages = {
  zh: {
    calendar: syncZh,
    habit: featureZhHabit,
    assistant: featureZhAssistant,
    reminders: featureZhReminders,
  },
  en: {
    calendar: syncEn,
    habit: featureEnHabit,
    assistant: featureEnAssistant,
    reminders: featureEnReminders,
  },
};

const calendarAlarmImages = {
  zh: featureZhCalendarAlarm,
  en: featureEnCalendarAlarm,
};

function WindowBar() {
  return (
    <div className="product-window-bar">
      <span className="window-dots" aria-hidden="true"><i /><i /><i /></span>
      <span>GoalCal</span>
      <span className="window-status"><i />Web</span>
    </div>
  );
}

export function HeroProduct() {
  const { lang, t } = useI18n();
  return (
    <div className="hero-product">
      <div className="hero-product-glow" />
      <div className="product-window hero-window real-product-window">
        <WindowBar />
        <img
          className="hero-screenshot"
          src={heroImages[lang]}
          alt={t('GoalCal 的「今天」页，显示首次 10 公里目标的训练时间和开始按钮', 'GoalCal Today with the time and Start button for the first 10K training session')}
        />
      </div>
    </div>
  );
}

export function StoryVisual({ story }: { story: StoryIndex }) {
  const { lang, t } = useI18n();
  const labels = [
    t('首个 10 公里目标的真实追问，包括当前跑步基础和目标期限', 'Follow-up questions for the first 10K goal, including current running level and target date'),
    t('修改后的 10 公里草稿，第一步是轻松慢跑，下方显示重新生成和保存操作', 'Revised 10K draft with an easy jog first, plus regeneration and save controls'),
    t('轻松慢跑的推荐时段，点击时段即可安排', 'Suggested times for the easy jog. Selecting a slot schedules it.'),
    t('今天 17:45–18:10 的轻松慢跑，以及开始和完成按钮', 'Today’s easy jog from 17:45 to 18:10 with Start and Complete buttons'),
    t('真实周计划预览，四个已选事项及统一采纳按钮', 'The real weekly plan preview with four selected sessions and one Accept button'),
  ];
  return (
    <div className={`story-visual story-visual-${story}`} key={story}>
      {story === 0 && (
        <div className="idea-bubble story-input-context">
          <p>{t('“我要在 8 周内完成人生第一个 10 公里。”', '“I want to run my first 10K in eight weeks.”')}</p>
        </div>
      )}
      <img className="story-screenshot" src={storyImages[lang][story]} alt={labels[story]} loading="lazy" />
    </div>
  );
}

function FeatureScreenshot({ name, alt }: { name: FeatureName; alt: string }) {
  const { lang } = useI18n();
  return (
    <div className={`real-feature-visual feature-visual-${name}`}>
      <img src={featureImages[lang][name]} alt={alt} loading="lazy" />
    </div>
  );
}

export function CalendarSyncDemo() {
  const { t } = useI18n();
  return <FeatureScreenshot name="calendar" alt={t('真实日历中的外部日程、目标与习惯', 'Real calendar showing external events, goals, and habits')} />;
}

export function HabitDemo() {
  const { t } = useI18n();
  return <FeatureScreenshot name="habit" alt={t('GoalCal 真实每周次数习惯页面', 'GoalCal’s real weekly-frequency habit view')} />;
}

export function AssistantDemo() {
  const { t } = useI18n();
  return <FeatureScreenshot name="assistant" alt={t('GoalCal 助手完整面板，展示剩余训练调整方案、确认按钮与对话输入区', 'The full GoalCal Assistant panel with a revised training plan, confirmation controls, and composer')} />;
}

export function ReminderDemo() {
  const { lang, t } = useI18n();
  return (
    <div className="reminder-showcase">
      <figure className="reminder-showcase-channel reminder-channel-calendar">
        <figcaption>{t('系统日历提醒', 'System calendar alerts')}</figcaption>
        <div className="reminder-channel-frame">
          <img src={calendarAlarmImages[lang]} alt={t('真实系统日历提醒设置：目标执行块提前 10 分钟提醒', 'Real system calendar alerts for goal blocks, set to 10 minutes before')} loading="lazy" />
        </div>
      </figure>
      <figure className="reminder-showcase-channel reminder-channel-email">
        <figcaption>{t('每周邮件复盘', 'Weekly email review')}</figcaption>
        <div className="reminder-channel-frame">
          <img src={featureImages[lang].reminders} alt={t('GoalCal 真实每周复盘邮件：本周完成 6 个目标块、3 次习惯，并提供安排下周入口', 'GoalCal’s real weekly review email with six completed goal blocks, three habit sessions, and a Plan Next Week action')} loading="lazy" />
        </div>
      </figure>
    </div>
  );
}
