const forms = [...document.querySelectorAll('#run-form, #closure-form')];
const feedback = document.querySelector('#run-feedback');
const submit = forms[0]?.querySelector('button[type="submit"]');

document.querySelector('#print')?.addEventListener('click', () => window.print());

forms.forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    delete feedback.dataset.error;
    feedback.value = '正在结算…';
    if (submit) submit.disabled = true;
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '结算失败，请重试。');
      window.location.assign(payload.location);
    } catch (error) {
      feedback.dataset.error = '';
      feedback.value = error.message || '请求失败，请重试。';
      if (submit) submit.disabled = false;
    }
  });
});
