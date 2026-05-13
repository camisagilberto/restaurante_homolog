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
          const { response, data } = await requestJSON('/carrinho/adicionar', { product_id: Number(productId), quantity });
          if (!response.ok || !data.success) throw new Error(data.message || 'Falha ao adicionar.');
          updateMenuSummary(data);
          setFeedback(feedback, data.message || 'Adicionado ao carrinho.', 'success');
          if (input) input.value = '0';
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

      const sync = (delta) => {
        const current = parseInt(input?.value || '0', 10) || 0;
        if (input) {
          input.value = String(Math.max(0, current + delta));
        }
      };

      const removeCardIfEmpty = () => {
        const remaining = document.querySelectorAll('[data-cart-item]').length;
        if (remaining === 0) {
          window.location.reload();
        }
      };

      dec?.addEventListener('click', () => sync(-1));
      inc?.addEventListener('click', () => sync(1));

      updateButton?.addEventListener('click', async () => {
        const quantity = Math.max(0, parseInt(input?.value || '0', 10) || 0);
        updateButton.disabled = true;
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
          updateButton.disabled = false;
          if (removeButton) removeButton.disabled = false;
          window.setTimeout(() => setFeedback(itemCard.querySelector('[data-feedback]'), '', ''), 2500);
        }
      });

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
      const customerName = form.querySelector('[name="customer_name"]')?.value || '';
      const notes = form.querySelector('[name="notes"]')?.value || '';
      const submitButton = form.querySelector('button[type="submit"]');
      submitButton.disabled = true;
      submitButton.textContent = 'Enviando...';

      try {
        const { response, data } = await requestJSON('/pedido/finalizar', {
          customer_name: customerName,
          notes,
        });
        if (!response.ok || !data.success) throw new Error(data.message || 'Não foi possível finalizar.');
        window.alert(data.message || 'Pedido enviado com sucesso.');
        window.location.href = data.redirect_url || '/mesa/1';
      } catch (error) {
        alert(error.message || 'Erro ao finalizar pedido.');
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = 'Finalizar pedido';
      }
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

  function initKitchenAccess() {
    const kitchenLink = document.querySelector('[data-kitchen-access]');
    if (!kitchenLink) return;

    kitchenLink.addEventListener('click', async (event) => {
      event.preventDefault();

      const senha = prompt('Digite a senha do admin para acessar a cozinha:');
      if (!senha) return;

      try {
        const { response, data } = await requestJSON('/cozinha/validar', { password: senha });

        if (!response.ok || !data.success) {
          throw new Error(data.message || 'Senha inválida.');
        }

        window.location.href = data.redirect_url || kitchenLink.href;
      } catch (error) {
        alert(error.message || 'Erro ao acessar cozinha.');
      }
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
    initKitchenAccess();
    initKitchenDelete();
  });
})();