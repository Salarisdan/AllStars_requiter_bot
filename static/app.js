const form = document.querySelector("[data-application-form]");
const statusNode = document.querySelector("[data-form-status]");
const revealItems = document.querySelectorAll(
  ".hero-copy, .hero-panel, .section-block, .feature-card, .timeline article, .faq-grid article, .panel-card, .application-form, .form-copy",
);
const applicationModal = document.querySelector("[data-application-modal]");
const openApplicationButtons = document.querySelectorAll("[data-open-application-modal]");
const modalCloseTargets = document.querySelectorAll("[data-modal-close]");
const applicationStage = document.querySelector("[data-application-stage]");
const verificationAlert = document.querySelector("[data-verification-alert]");
const verificationAlertClose = document.querySelector("[data-verification-alert-close]");
const verificationRadios = form ? Array.from(form.querySelectorAll("input[name='verification']")) : [];

function setStatus(message, kind = "") {
  if (!statusNode) {
    return;
  }

  statusNode.textContent = message;
  statusNode.classList.remove("success", "error");
  if (kind) {
    statusNode.classList.add(kind);
  }
}

function openApplicationModal() {
  if (!applicationModal) {
    return;
  }

  applicationModal.hidden = false;
  applicationModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");

  const firstVerification = form?.querySelector("input[name='verification']");
  window.setTimeout(() => {
    firstVerification?.focus();
  }, 0);
}

function closeApplicationModal() {
  if (!applicationModal) {
    return;
  }

  applicationModal.hidden = true;
  applicationModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");

  if (verificationAlert) {
    verificationAlert.hidden = true;
  }
}

function showVerificationAlert() {
  if (!verificationAlert) {
    return;
  }

  verificationAlert.hidden = false;
  const closeButton = verificationAlert.querySelector("[data-verification-alert-close]");
  window.setTimeout(() => {
    closeButton?.focus();
  }, 0);
}

function hideVerificationAlert() {
  if (verificationAlert) {
    verificationAlert.hidden = true;
  }
}

function syncVerificationGate() {
  if (!applicationStage) {
    return;
  }

  const accepted = Boolean(form?.querySelector("input[name='verification'][value='✅ Да']:checked"));
  applicationStage.hidden = !accepted;
  applicationStage.disabled = !accepted;
  applicationStage.setAttribute("aria-hidden", accepted ? "false" : "true");

  if (!accepted && verificationAlert && !verificationAlert.hidden) {
    return;
  }
}

if (form && statusNode) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (!form.querySelector("input[name='verification'][value='✅ Да']:checked")) {
      setStatus("Сначала нужно пройти верификацию. Без этого анкета не отправится.", "error");
      showVerificationAlert();
      return;
    }

    const selectedShifts = Array.from(form.querySelectorAll("input[name='shifts']:checked")).map((input) => input.value);
    if (!selectedShifts.length) {
      setStatus("Выбери хотя бы одну смену.", "error");
      return;
    }

    setStatus("Отправляем заявку в Telegram...");

    const submitButton = form.querySelector("button[type='submit']");
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    payload.shifts = selectedShifts;

    if (submitButton) {
      submitButton.disabled = true;
      submitButton.style.opacity = "0.82";
    }

    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
        },
        body: JSON.stringify(payload),
      });

      const data = await response.json();

      if (!response.ok || !data.ok) {
        throw new Error(data.error || "Не удалось отправить заявку");
      }

      form.reset();
      syncVerificationGate();
      setStatus("Заявка отправлена. Мы уже увидели ее в Telegram.", "success");
    } catch (error) {
      setStatus(error.message || "Ошибка отправки", "error");
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.style.opacity = "1";
      }
    }
  });

  verificationRadios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.value === "❌ Нет" && radio.checked) {
        setStatus("Верификация обязательна. Без нее анкету продолжить нельзя.", "error");
        showVerificationAlert();
      } else if (radio.value === "✅ Да" && radio.checked) {
        hideVerificationAlert();
        setStatus("");
      }

      syncVerificationGate();
    });
  });
}

openApplicationButtons.forEach((button) => {
  button.addEventListener("click", openApplicationModal);
});

modalCloseTargets.forEach((target) => {
  target.addEventListener("click", closeApplicationModal);
});

verificationAlertClose?.addEventListener("click", hideVerificationAlert);

if (applicationModal) {
  applicationModal.addEventListener("click", (event) => {
    if (event.target === applicationModal) {
      closeApplicationModal();
    }
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && applicationModal && !applicationModal.hidden) {
    closeApplicationModal();
  }
});

syncVerificationGate();

if ("IntersectionObserver" in window && revealItems.length) {
  const revealObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          revealObserver.unobserve(entry.target);
        }
      }
    },
    { threshold: 0.14, rootMargin: "0px 0px -8% 0px" },
  );

  revealItems.forEach((item) => revealObserver.observe(item));
}