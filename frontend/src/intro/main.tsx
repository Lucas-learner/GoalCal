import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nProvider } from '../i18n';
import { IntroPage } from './IntroPage';
import './intro.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <I18nProvider>
      <IntroPage />
    </I18nProvider>
  </StrictMode>,
);
