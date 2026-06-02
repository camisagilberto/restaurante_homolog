(function () {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

  async function requestJSON(url, payload) {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': csrfToken,
      },
      body: JSON.stringify({ ...payload, csrf_token: csrfToken }),
    });

    let data = {};
    try {
      data = await response.json();
    } catch (_) {}
    return { response, data };
  }

  function formatMoney(value) {
    return Number(value || 0).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function setFeedback(node, message, kind) {
    if (!node) return;
    node.textContent = message || '';
    node.dataset.state = kind || '';
  }

  function updateMoneyTargets(amount, selectors) {
    selectors.forEach((selector) => {
      const node = document.querySelector(selector);
      if (node) node.textContent = `R$ ${formatMoney(amount ?? 0)}`;
    });
  }

  function updateQuantityTargets(quantity, selectors) {
    selectors.forEach((selector) => {
      const node = document.querySelector(selector);
      if (node) node.textContent = String(quantity ?? 0);
    });
  }

  function updateMenuSummary(data) {
    updateQuantityTargets(data.cart_quantity ?? 0, ['#cart-count', '#menu-cart-quantity']);
    updateMoneyTargets(data.cart_total ?? 0, ['#menu-cart-total']);
  }

  function updateCartSummary(data) {
    updateQuantityTargets(data.cart_quantity ?? 0, ['#cart-quantity']);
    updateMoneyTargets(data.cart_total ?? 0, ['#cart-total']);
    updateMenuSummary(data);
  }

  function initMenu() {
    document.querySelectorAll('[data-product-id]').forEach((card) => {
      const input = card.querySelector('[data-qty-input]');
      const dec = card.querySelector('[data-qty-dec]');
      const inc = card.querySelector('[data-qty-inc]');
      const add = card.querySelector('[data-add-to-cart]');
      const feedback = card.querySelector('[data-feedback]');
      const productId = card.dataset.productId;

      const sync = (delta) => {
        const current = parseInt(input?.value || '0', 10) || 0;
        if (input) {
          input.value = String(Math.max(0, current + delta));
        }
      };

      dec?.addEventListener('click', () => sync(-1));
      inc?.addEventListener('click', () => sync(1));

      add?.addEventListener('click', async () => {
        const quantity = Math.max(0, parseInt(input?.value || '0', 10) || 0);

        if (quantity <= 0) {
          setFeedback(feedback, 'Selecione pelo menos 1 item.', 'error');
          window.setTimeout(() => setFeedback(feedback, '', ''), 2500);
          return;
        }

        add.disabled = true;
        setFeedback(feedback, 'Adicionando...', 'loading');

        try {
          const { response, data } = await requestJSON('/carrinho/adicionar', {
            product_id: Number(productId),
            quantity,
          });

          if (!response.ok || !data.success) {
            throw new Error(data.message || 'Falha ao adicionar.');
          }

          updateMenuSummary(data);
          setFeedback(feedback, data.message || 'Adicionado ao carrinho.', 'success');
        } catch (error) {
          setFeedback(feedback, error.message || 'Não foi possível adicionar.', 'error');
        } finally {
          add.disabled = false;
          window.setTimeout(() => setFeedback(feedback, '', ''), 2500);
        }
      });
    });
  }

  function initCart() {
    const form = document.getElementById('finalize-order-form');

    document.querySelectorAll('[data-cart-item]').forEach((itemCard) => {
      const input = itemCard.querySelector('[data-cart-qty]');
      const dec = itemCard.querySelector('[data-cart-dec]');
      const inc = itemCard.querySelector('[data-cart-inc]');
      const updateButton = itemCard.querySelector('[data-cart-update]');
      const removeButton = itemCard.querySelector('[data-cart-remove]');
      const productId = itemCard.dataset.productId;

      let updateTimer = null;

      const removeCardIfEmpty = () => {
        const remaining = document.querySelectorAll('[data-cart-item]').length;
        if (remaining === 0) {
          window.location.reload();
        }
      };

      const persistQuantity = async () => {
        const quantity = Math.max(0, parseInt(input?.value || '0', 10) || 0);

        if (updateButton) updateButton.disabled = true;
        if (removeButton) removeButton.disabled = true;
        setFeedback(itemCard.querySelector('[data-feedback]'), 'Atualizando...', 'loading');

        try {
          const { response, data } = await requestJSON('/carrinho/atualizar', {
            product_id: Number(productId),
            quantity,
          });

          if (!response.ok || !data.success) {
            throw new Error(data.message || 'Não foi possível atualizar.');
          }

          updateCartSummary(data);

          if (data.removed || quantity <= 0) {
            itemCard.remove();
            removeCardIfEmpty();
          } else if (input) {
            input.value = String(data.quantity ?? quantity);
          }

          setFeedback(itemCard.querySelector('[data-feedback]'), 'Carrinho atualizado.', 'success');
        } catch (error) {
          setFeedback(itemCard.querySelector('[data-feedback]'), error.message || 'Erro ao atualizar.', 'error');
        } finally {
          if (updateButton) updateButton.disabled = false;
          if (removeButton) removeButton.disabled = false;
          window.setTimeout(() => setFeedback(itemCard.querySelector('[data-feedback]'), '', ''), 1800);
        }
      };

      const scheduleUpdate = () => {
        window.clearTimeout(updateTimer);
        updateTimer = window.setTimeout(() => {
          persistQuantity();
        }, 350);
      };

      const sync = (delta) => {
        const current = parseInt(input?.value || '0', 10) || 0;
        if (input) {
          input.value = String(Math.max(0, current + delta));
          scheduleUpdate();
        }
      };

      dec?.addEventListener('click', () => sync(-1));
      inc?.addEventListener('click', () => sync(1));
      input?.addEventListener('change', scheduleUpdate);
      input?.addEventListener('blur', scheduleUpdate);

      updateButton?.addEventListener('click', persistQuantity);

      removeButton?.addEventListener('click', async () => {
        updateButton && (updateButton.disabled = true);
        removeButton.disabled = true;
        setFeedback(itemCard.querySelector('[data-feedback]'), 'Removendo...', 'loading');

        try {
          const { response, data } = await requestJSON('/carrinho/excluir', {
            product_id: Number(productId),
          });

          if (!response.ok || !data.success) {
            throw new Error(data.message || 'Não foi possível remover.');
          }

          updateCartSummary(data);
          itemCard.remove();
          removeCardIfEmpty();
        } catch (error) {
          setFeedback(itemCard.querySelector('[data-feedback]'), error.message || 'Erro ao remover.', 'error');
        } finally {
          updateButton && (updateButton.disabled = false);
          removeButton.disabled = false;
          window.setTimeout(() => setFeedback(itemCard.querySelector('[data-feedback]'), '', ''), 2500);
        }
      });
    });

    form?.addEventListener('submit', async (event) => {
      event.preventDefault();

      const notes = form.querySelector('[name="notes"]')?.value || '';
      const submitButton = form.querySelector('button[type="submit"]');

      submitButton.disabled = true;
      submitButton.textContent = 'Gerando Pix...';

      try {
        const { response, data } = await requestJSON('/pedido/finalizar', {
          notes,
        });

        if (!response.ok || !data.success) {
          throw new Error(data.message || 'Não foi possível gerar o Pix.');
        }

        window.location.href = data.payment_url || data.redirect_url || '/mesa/1';
      } catch (error) {
        alert(error.message || 'Erro ao gerar Pix.');
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = 'Gerar Pix';
      }
    });
  }

  function initCouponCodeConfirm() {
    document.querySelectorAll('[data-coupon-code-confirm]').forEach((form) => {
      form.addEventListener('submit', (event) => {
        const confirmed = confirm('O código numérico terá validade de apenas 10 minutos. Gere o código somente se você for fazer o pagamento neste intervalo. Tem certeza que deseja gerar o código agora?');
        if (!confirmed) {
          event.preventDefault();
        }
      });
    });
  }

  function initTableEditor() {
    const trigger = document.querySelector('[data-open-table-editor]');
    if (!trigger) return;

    trigger.addEventListener('click', async () => {
      const password = prompt('Digite a senha do admin para editar a mesa:');
      if (!password) return;

      const currentTable = document.getElementById('table-number-label')?.textContent?.trim() || '';
      const tableNumber = prompt('Informe o novo número da mesa:', currentTable);
      if (!tableNumber) return;

      try {
        const { response, data } = await requestJSON('/mesa/editar', {
          table_number: tableNumber,
          manager_password: password,
        });

        if (!response.ok || !data.success) {
          throw new Error(data.message || 'Não foi possível editar a mesa.');
        }

        window.location.href = data.redirect_url || `/mesa/${encodeURIComponent(data.table_number || tableNumber)}`;
      } catch (error) {
        alert(error.message || 'Erro ao editar a mesa.');
      }
    });
  }

  function askPasswordModal(message) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'password-modal-overlay';
      overlay.innerHTML = `
        <div class="password-modal" role="dialog" aria-modal="true" aria-labelledby="password-modal-title">
          <h3 id="password-modal-title">Senha necessária</h3>
          <p>${message}</p>
          <input type="password" autocomplete="current-password" placeholder="Digite a senha" />
          <div class="password-modal-actions">
            <button type="button" class="btn btn-secondary" data-cancel>Cancelar</button>
            <button type="button" class="btn btn-primary" data-confirm>Continuar</button>
          </div>
        </div>
      `;

      document.body.appendChild(overlay);
      const input = overlay.querySelector('input');
      const cancel = overlay.querySelector('[data-cancel]');
      const confirm = overlay.querySelector('[data-confirm]');

      const close = (value) => {
        overlay.remove();
        resolve(value);
      };

      cancel?.addEventListener('click', () => close(''));
      overlay.addEventListener('click', (event) => {
        if (event.target === overlay) close('');
      });
      confirm?.addEventListener('click', () => close((input?.value || '').trim()));
      input?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') close((input.value || '').trim());
        if (event.key === 'Escape') close('');
      });

      window.setTimeout(() => input?.focus(), 50);
    });
  }

  function initNavigationAccordion() {
    document.querySelectorAll('[data-nav-accordion]').forEach((nav) => {
      const groups = Array.from(nav.querySelectorAll('details'));
      groups.forEach((group) => {
        group.removeAttribute('open');
        group.addEventListener('toggle', () => {
          if (!group.open) return;
          groups.forEach((other) => {
            if (other !== group) other.removeAttribute('open');
          });
        });
      });
    });
  }

  
  function initPasswordConfirmForms() {
    document.querySelectorAll('[data-password-confirm]').forEach((form) => {
      form.addEventListener('submit', (event) => {
        const message = form.dataset.passwordConfirm || 'Digite sua senha para confirmar.';
        const confirmed = confirm('Tem certeza que deseja continuar?');
        if (!confirmed) {
          event.preventDefault();
          return;
        }

        const password = prompt(message);
        if (!password) {
          event.preventDefault();
          return;
        }

        const input = form.querySelector('[name="manager_password"]');
        if (input) input.value = password;
      });
    });
  }

  function initKitchenDelete() {
    const deleteButton = document.querySelector('[data-delete-orders]');
    if (!deleteButton) return;

    deleteButton.addEventListener('click', async () => {
      const confirmed = confirm('Tem certeza que deseja apagar todo histórico de pedidos?');
      if (!confirmed) return;

      const password = prompt('Digite a senha do admin para apagar o histórico de pedidos:');
      if (!password) return;

      try {
        const { response, data } = await requestJSON('/cozinha/apagar-pedidos', { password });

        if (!response.ok || !data.success) {
          throw new Error(data.message || 'Não foi possível apagar os pedidos.');
        }

        window.location.href = data.redirect_url || '/cozinha/';
      } catch (error) {
        alert(error.message || 'Erro ao apagar pedidos.');
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initMenu();
    initCart();
    initTableEditor();
    initNavigationAccordion();
    initPasswordConfirmForms();
    initKitchenDelete();
    initCouponCodeConfirm();
  });
})();

// Confirmação para gerar código de cupom rastreável QRTotem.
document.addEventListener('submit', function (event) {
  const form = event.target;
  if (!form || !form.matches('[data-confirm-coupon-code]')) {
    return;
  }

  const message = form.getAttribute('data-confirm-coupon-code') || 'Tem certeza que deseja gerar o código agora?';
  if (!window.confirm(message)) {
    event.preventDefault();
  }
});

// Validação do campo "Indicado por" no primeiro cadastro do restaurante.
document.addEventListener('DOMContentLoaded', function () {
  const input = document.querySelector('[data-referrer-lookup-url]');
  const status = document.getElementById('indicated_by_status');
  if (!input || !status) return;

  let timer = null;
  const setStatus = (message, type) => {
    status.textContent = message || '';
    status.classList.remove('success', 'error', 'info');
    status.classList.add(type || 'info');
  };

  const validate = async () => {
    const value = (input.value || '').trim();
    if (!value) {
      setStatus('', 'info');
      return;
    }

    setStatus('Verificando cliente...', 'info');

    try {
      const url = new URL(input.getAttribute('data-referrer-lookup-url'), window.location.origin);
      url.searchParams.set('identifier', value);
      const response = await fetch(url.toString(), { headers: { 'Accept': 'application/json' } });
      const data = await response.json();
      if (data.found) {
        setStatus(data.message || 'Cliente encontrado.', 'success');
      } else {
        setStatus(data.message || 'Cliente ainda não criado.', 'error');
      }
    } catch (error) {
      setStatus('Não foi possível verificar agora. O cadastro validará ao avançar.', 'info');
    }
  };

  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(validate, 500);
  });

  input.addEventListener('blur', validate);
});
